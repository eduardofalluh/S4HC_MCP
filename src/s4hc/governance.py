"""Governance controls required by SAP's third-party MCP reference
architecture (architecture.learning.sap.com/docs/ref-arch/137800) and
API Policy v.4.2026a §2.1 / §2.2.2:

* backend-enforced data scope (allowed company codes / cost centers),
* audit log of every tool call (tool, scope keys, duration, outcome — never
  credentials, never row data),
* MCP-level rate limit per connection, plus a global ceiling, that stays below
  the SAP API's own limits.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import OrderedDict, deque
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
        if request is None:
            return None
        v = request.headers.get(name)
        return v if v is not None else None
    except Exception:  # noqa: BLE001
        return None


def _csv(value: str) -> frozenset[str]:
    return frozenset(validate_id(v, "allowlist entry") for v in value.split(",") if v.strip())


def _allowlist(ctx: Context | None, header: str, env: str, what: str) -> frozenset[str]:
    """Effective allowlist. The deployment's env list is a ceiling a connection
    header can only narrow; a header that is present but names nothing, or
    names nothing inside the ceiling, is refused rather than read as 'no limit'."""
    ceiling = _csv(os.getenv(env) or "")
    raw = _header(ctx, HEADER_PREFIX + header)
    if raw is None:
        return ceiling
    # Present means "scope this connection": a blank value (an unset template
    # variable) or ',' is refused, never read as "no limit".
    requested = _csv(raw)
    if not requested:
        raise S4HCError(f"x-s4hc-{header} is present but lists no {what}", "UNAUTHORIZED_SCOPE")
    effective = requested & ceiling if ceiling else requested
    if not effective:
        raise S4HCError(f"x-s4hc-{header} names no {what} inside this deployment's {env}", "UNAUTHORIZED_SCOPE")
    return effective


@dataclass(frozen=True)
class Scope:
    company_codes: frozenset[str]  # empty = unrestricted
    cost_centers: frozenset[str]

    @property
    def restricted(self) -> bool:
        return bool(self.company_codes or self.cost_centers)


def get_scope(ctx: Context | None = None) -> Scope:
    return Scope(
        company_codes=_allowlist(ctx, "allowed-company-codes", "S4HC_ALLOWED_COMPANY_CODES", "company codes"),
        cost_centers=_allowlist(ctx, "allowed-cost-centers", "S4HC_ALLOWED_COST_CENTERS", "cost centers"),
    )


def enforce_company_code(ctx: Context | None, company_code: str | None) -> str | None:
    """Return the company code to query with. Raises if outside the allowlist.
    With an allowlist of exactly one code and no request value, that code is
    applied automatically; with several and no request value, the caller must
    also apply allowed_company_codes_for_filter()."""
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


def scope_columns(ctx: Context | None, cc_key: str | None = "CostCenter", co_key: str | None = "CompanyCode") -> list[str]:
    """Columns a listing must select so filter_rows_to_scope can check every row."""
    scope = get_scope(ctx)
    cols = []
    if scope.company_codes and co_key:
        cols.append(co_key)
    if scope.cost_centers and cc_key:
        cols.append(cc_key)
    return cols


def filter_rows_to_scope(ctx: Context | None, rows: list[dict], cc_key: str | None = "CostCenter",
                         co_key: str | None = "CompanyCode") -> list[dict]:
    """Post-filter for entity sets where the allowlist cannot be pushed into
    $filter. Fails closed: a row missing a scoped column is dropped. Pass
    cc_key/co_key=None for an entity that has no such column at all."""
    scope = get_scope(ctx)
    if not scope.restricted:
        return rows
    out = []
    for r in rows:
        if scope.cost_centers and cc_key and str(r.get(cc_key, "")) not in scope.cost_centers:
            continue
        if scope.company_codes and co_key and str(r.get(co_key, "")) not in scope.company_codes:
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
    """Logs every tool call and rate-limits it.

    Trusted connections (the server's own env credentials, or credentials SAP has
    answered 2xx for) get S4HC_RATE_LIMIT_PER_MIN (120) each and share
    S4HC_RATE_LIMIT_GLOBAL_PER_MIN (600). Unverified connections get the same
    per-connection limit but all share S4HC_RATE_LIMIT_UNVERIFIED_PER_MIN (60),
    in a separate bounded map: invented credentials can neither drain the
    trusted budget nor evict a trusted connection's bucket."""

    MAX_BUCKETS = 1024

    def __init__(self) -> None:
        self.per_connection = int(os.getenv("S4HC_RATE_LIMIT_PER_MIN", "120"))
        self.global_bucket = _TokenBucket(int(os.getenv("S4HC_RATE_LIMIT_GLOBAL_PER_MIN", "600")))
        self.unverified_bucket = _TokenBucket(int(os.getenv("S4HC_RATE_LIMIT_UNVERIFIED_PER_MIN", "60")))
        self._buckets: OrderedDict[str, _TokenBucket] = OrderedDict()
        self._unverified_buckets: OrderedDict[str, _TokenBucket] = OrderedDict()
        self._lock = threading.Lock()

    def _bucket(self, key: str, buckets: OrderedDict[str, _TokenBucket] | None = None) -> _TokenBucket:
        buckets = self._buckets if buckets is None else buckets
        with self._lock:
            b = buckets.get(key)
            if b is None:
                b = buckets[key] = _TokenBucket(self.per_connection)
                while len(buckets) > self.MAX_BUCKETS:
                    buckets.popitem(last=False)
            else:
                buckets.move_to_end(key)
            return b

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        from .dependencies import rate_limit_key

        name = getattr(context.message, "name", "?")
        args = getattr(context.message, "arguments", None) or {}
        scope_args = {k: args[k] for k in SCOPE_ARG_KEYS if k in args and args[k] not in (None, [], "")}
        ctx = context.fastmcp_context
        caller = None
        try:
            req = ctx.request_context.request  # type: ignore[union-attr]
            caller = req.headers.get("x-user-email") or req.headers.get("x-s4hc-user")
        except Exception:  # noqa: BLE001
            pass
        conn, trusted = rate_limit_key(ctx)
        if trusted:
            allowed = self._bucket(conn).allow() and self.global_bucket.allow()
        else:
            allowed = self.unverified_bucket.allow() and self._bucket(conn, self._unverified_buckets).allow()
        tag = f"conn={conn[:8]} trusted={trusted}"
        if not allowed:
            log.warning("audit tool=%s caller=%s %s outcome=rate_limited", name, caller, tag)
            raise S4HCError("Rate limit exceeded for this connection; retry in a minute", "SAP_RATE_LIMITED")
        t0 = time.monotonic()
        try:
            result = await call_next(context)
        except Exception as exc:
            log.info("audit tool=%s caller=%s %s args=%s ms=%d outcome=error:%s",
                     name, caller, tag, scope_args, (time.monotonic() - t0) * 1000, exc.__class__.__name__)
            raise
        log.info("audit tool=%s caller=%s %s args=%s ms=%d outcome=ok",
                 name, caller, tag, scope_args, (time.monotonic() - t0) * 1000)
        return result
