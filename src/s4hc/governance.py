"""Governance controls required by SAP's third-party MCP reference
architecture (architecture.learning.sap.com/docs/ref-arch/137800) and
API Policy v.4.2026a §2.1 / §2.2.2:

* backend-enforced data scope (allowed company codes / cost centers),
* audit log of every tool call (tool, scope keys, duration, outcome — never
  credentials, never row data),
* MCP-level rate limit that stays below the SAP API's own limits.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass

from fastmcp import Context
from fastmcp.server.middleware import Middleware, MiddlewareContext

from .client import S4HCError, validate_id

log = logging.getLogger("s4hc-mcp.audit")

HEADER_PREFIX = "x-s4hc-"
SCOPE_ARG_KEYS = (
    "company_code", "controlling_area", "cost_centers", "cost_center", "profit_centers", "gl_accounts",
    "fiscal_year", "compare_fiscal_year", "period_from", "period_to", "fiscal_year_from", "fiscal_year_to",
    "dimension", "service", "entity_set",
)


def _header(ctx: Context | None, name: str) -> str | None:
    try:
        request = ctx.request_context.request  # type: ignore[union-attr]
        v = request.headers.get(name) if request is not None else None
        return v.strip() if v else None
    except Exception:  # noqa: BLE001
        return None


def _csv(value: str | None) -> set[str]:
    return {validate_id(v, "allowlist entry") for v in (value or "").split(",") if v.strip()}


@dataclass(frozen=True)
class Scope:
    company_codes: frozenset[str]  # empty = unrestricted
    cost_centers: frozenset[str]

    @property
    def restricted(self) -> bool:
        return bool(self.company_codes or self.cost_centers)


def get_scope(ctx: Context | None = None) -> Scope:
    return Scope(
        company_codes=frozenset(_csv(_header(ctx, HEADER_PREFIX + "allowed-company-codes") or os.getenv("S4HC_ALLOWED_COMPANY_CODES"))),
        cost_centers=frozenset(_csv(_header(ctx, HEADER_PREFIX + "allowed-cost-centers") or os.getenv("S4HC_ALLOWED_COST_CENTERS"))),
    )


def enforce_company_code(ctx: Context | None, company_code: str | None) -> str | None:
    """Return the company code to query with. Raises if outside the allowlist.
    With an allowlist of exactly one code and no request value, that code is
    applied automatically; with several and no request value, the caller must
    use enforce_company_codes_filter()."""
    scope = get_scope(ctx)
    if not scope.company_codes:
        return company_code
    if company_code:
        if validate_id(company_code, "company_code") not in scope.company_codes:
            raise S4HCError(f"company_code {company_code!r} is outside this connection's allowed scope", "UNAUTHORIZED_SCOPE")
        return company_code
    if len(scope.company_codes) == 1:
        return next(iter(scope.company_codes))
    return None


def allowed_company_codes_for_filter(ctx: Context | None, company_code: str | None) -> list[str] | None:
    """List of company codes to OR into a filter when the request named none."""
    scope = get_scope(ctx)
    if company_code or not scope.company_codes:
        return None
    return sorted(scope.company_codes)


def enforce_cost_centers(ctx: Context | None, cost_centers: list[str] | None) -> list[str] | None:
    """Return the cost center list to query with (narrowed to the allowlist)."""
    scope = get_scope(ctx)
    if not scope.cost_centers:
        return cost_centers
    if not cost_centers:
        return sorted(scope.cost_centers)
    bad = [c for c in cost_centers if validate_id(c, "cost_center") not in scope.cost_centers]
    if bad:
        raise S4HCError(f"cost centers {bad} are outside this connection's allowed scope", "UNAUTHORIZED_SCOPE")
    return cost_centers


def filter_rows_to_scope(ctx: Context | None, rows: list[dict], cc_key: str = "CostCenter", co_key: str = "CompanyCode") -> list[dict]:
    """Post-filter for entity sets where the allowlist cannot be pushed into $filter."""
    scope = get_scope(ctx)
    if not scope.restricted:
        return rows
    out = []
    for r in rows:
        if scope.cost_centers and cc_key in r and str(r.get(cc_key)) not in scope.cost_centers:
            continue
        if scope.company_codes and co_key in r and str(r.get(co_key)) not in scope.company_codes:
            continue
        out.append(r)
    return out


class _TokenBucket:
    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self._hits: deque[float] = deque()
        self._lock = threading.Lock()

    def allow(self) -> bool:
        if self.per_minute <= 0:
            return True
        now = time.monotonic()
        with self._lock:
            while self._hits and now - self._hits[0] > 60:
                self._hits.popleft()
            if len(self._hits) >= self.per_minute:
                return False
            self._hits.append(now)
            return True


class AuditAndRateLimitMiddleware(Middleware):
    """Logs every tool call and enforces S4HC_RATE_LIMIT_PER_MIN (default 120)."""

    def __init__(self) -> None:
        self.bucket = _TokenBucket(int(os.getenv("S4HC_RATE_LIMIT_PER_MIN", "120")))

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        name = getattr(context.message, "name", "?")
        args = getattr(context.message, "arguments", None) or {}
        scope_args = {k: args[k] for k in SCOPE_ARG_KEYS if k in args and args[k] not in (None, [], "")}
        caller = None
        try:
            req = context.fastmcp_context.request_context.request  # type: ignore[union-attr]
            caller = req.headers.get("x-user-email") or req.headers.get("x-s4hc-user")
        except Exception:  # noqa: BLE001
            pass
        if not self.bucket.allow():
            log.warning("audit tool=%s caller=%s outcome=rate_limited", name, caller)
            raise S4HCError("Rate limit exceeded for this MCP server; retry in a minute", "SAP_RATE_LIMITED")
        t0 = time.monotonic()
        try:
            result = await call_next(context)
        except Exception as exc:
            log.info("audit tool=%s caller=%s args=%s ms=%d outcome=error:%s",
                     name, caller, scope_args, (time.monotonic() - t0) * 1000, exc.__class__.__name__)
            raise
        log.info("audit tool=%s caller=%s args=%s ms=%d outcome=ok", name, caller, scope_args, (time.monotonic() - t0) * 1000)
        return result
