"""
SAP S/4HANA Cloud (Public Edition) MCP Server — cost center analysis.

Run
---
    python server.py                       # stdio (Claude Desktop, Cursor, Inspector)
    python server.py --http --port 8000    # Streamable HTTP for Syntax GenAI Studio

Auth
----
Read-only. Uses an S/4HANA Cloud *communication user* on the API host
(https://<tenant>-api.s4hana.cloud.sap) with either Basic auth or an X.509
client certificate. Credentials come from env vars (see .env.example) or
per-request headers x-s4hc-base-url / x-s4hc-user / x-s4hc-password for
multi-tenant hosted deployments. Nothing is hard-coded.

Required communication arrangements on the tenant
--------------------------------------------------
    API_JOURNALENTRYITEMBASIC_SRV   → Journal Entry Item read scenario (REQUIRED:
                                      actuals + company codes, cost/profit centers, G/L accounts)
    API_COSTCENTER_SRV              → SAP_COM_0087 (OPTIONAL: cost center names/descriptions)
    API_GLACCOUNTLINEITEM           → G/L Account Line Items read scenario (OPTIONAL: posting-level detail)
    API_FINPLANNINGENTRYITEM_SRV    → Financial Planning Entry Item read (OPTIONAL: actual vs plan)

Governance (SAP third-party MCP reference architecture 137800, API Policy v.4.2026a)
-----------------------------------------------------------------------------------
  * read-only (GET only), input validation on every ID / field
  * backend data scope: S4HC_ALLOWED_COMPANY_CODES / S4HC_ALLOWED_COST_CENTERS
    (or x-s4hc-allowed-company-codes / x-s4hc-allowed-cost-centers per connection)
  * audit log per tool call, MCP rate limit S4HC_RATE_LIMIT_PER_MIN (default 120)
  * paging + hard row ceiling, retry/backoff that honours Retry-After
Run s4hc_health(live=true) to confirm what responds.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from dotenv import load_dotenv
from fastmcp import FastMCP
from starlette.middleware import Middleware
from starlette.types import ASGIApp, Receive, Scope, Send

from src.s4hc.governance import AuditAndRateLimitMiddleware
from src.s4hc.tools import actuals, analysis, cost_centers, health, master_data, odata_read, plan, postings

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("s4hc-mcp")


class _TrustProxyMiddleware:
    """Rewrite Host to 'localhost' for k8s/ingress deployments (same as CALM MCP)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = [(k, v) for k, v in scope.get("headers", []) if k.lower() != b"host"]
            headers.append((b"host", b"localhost"))
            scope = {**scope, "headers": headers}
        await self.app(scope, receive, send)


class _InboundKeyMiddleware:
    """Optional caller authentication for the HTTP endpoint (trust boundary
    AI client → MCP). When S4HC_MCP_API_KEY is set, every request must carry
    'Authorization: Bearer <key>' or 'x-api-key: <key>'. Constant-time compare.
    For full OAuth (issuer/audience validation) put the server behind the
    platform's OAuth proxy or the Integration Suite MCP Gateway."""

    def __init__(self, app: ASGIApp) -> None:
        import hmac

        self.app = app
        self.key = os.getenv("S4HC_MCP_API_KEY", "").encode()
        self._eq = hmac.compare_digest

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and self.key:
            h = {k.lower(): v for k, v in scope.get("headers", [])}
            auth = h.get(b"authorization", b"")
            supplied = auth[7:] if auth[:7].lower() == b"bearer " else h.get(b"x-api-key", b"")
            if not supplied or not self._eq(supplied, self.key):
                body = b'{"error":"unauthorized"}'
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json"), (b"www-authenticate", b"Bearer")]})
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


mcp = FastMCP("sap-s4hana-cloud")

# Audit log of every tool call + MCP-level rate limit (SAP ref-arch 137800 / API Policy §2.1)
mcp.add_middleware(AuditAndRateLimitMiddleware())

health.register(mcp)         # s4hc_health, fiscal context lives in analysis
master_data.register(mcp)    # list_company_codes, list_profit_centers, list_gl_accounts
cost_centers.register(mcp)   # list_cost_centers (with fallback)
actuals.register(mcp)        # get_cost_center_actuals, cost_center_summary
analysis.register(mcp)       # fiscal_context, actuals_breakdown, compare_cost_center_periods, cost_center_trend, export_actuals_csv
postings.register(mcp)       # get_cost_center_postings (API_GLACCOUNTLINEITEM, optional)
plan.register(mcp)           # get_actual_vs_plan (API_FINPLANNINGENTRYITEM_SRV, optional)
odata_read.register(mcp)     # describe_service, s4hc_odata_read


def _startup_check() -> None:
    from src.s4hc.dependencies import connection_info

    info = connection_info(None)
    if info["auth_mode"] == "none":
        log.warning("No S4HC_USER/S4HC_PASSWORD or client certificate set — expecting per-request x-s4hc-* headers.")
    else:
        log.info("S/4HANA Cloud target %s (auth=%s, ledger=%s)", info["base_url"], info["auth_mode"], info["default_ledger"])


def main() -> None:
    parser = argparse.ArgumentParser(description="SAP S/4HANA Cloud MCP server")
    parser.add_argument("--http", action="store_true", help="Serve over Streamable HTTP instead of stdio.")
    parser.add_argument("--host", default=os.getenv("MCP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("MCP_PORT", "8000")))
    args = parser.parse_args()

    _startup_check()

    if args.http:
        middleware = [Middleware(_InboundKeyMiddleware)]
        if os.getenv("MCP_TRUST_PROXY", "true").lower() in ("1", "true", "yes"):
            middleware.append(Middleware(_TrustProxyMiddleware))
        if os.getenv("S4HC_MCP_API_KEY"):
            log.info("Inbound API key required on /mcp")
        else:
            log.warning("S4HC_MCP_API_KEY not set — /mcp accepts any caller; rely on network/platform auth")
        log.info("Starting S/4HANA Cloud MCP on http://%s:%s/mcp", args.host, args.port)
        mcp.run(transport="streamable-http", host=args.host, port=args.port, middleware=middleware)
    else:
        log.info("Starting S/4HANA Cloud MCP on stdio")
        mcp.run()


if __name__ == "__main__":
    main()
