"""Resolve an S4HCClient and request-level defaults.

Resolution order (same idea as the CALM MCP):
  1. Per-request headers  x-s4hc-base-url / x-s4hc-user / x-s4hc-password
                          x-s4hc-ledger / x-s4hc-language / x-s4hc-controlling-area
     (multi-tenant GenAI Studio deployments inject these per connection)
  2. Environment          S4HC_BASE_URL / S4HC_USER / S4HC_PASSWORD or client cert
                          S4HC_LEDGER / S4HC_LANGUAGE / S4HC_CONTROLLING_AREA
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Callable

from fastmcp import Context

from . import config
from .client import S4HCClient

_cached: dict[str, S4HCClient] = {}
_master_cache: dict[str, tuple[float, Any]] = {}
MASTER_TTL_SECONDS = int(os.getenv("S4HC_MASTER_CACHE_TTL", "600"))

HEADER_PREFIX = "x-s4hc-"


def _header(ctx: Context | None, name: str) -> str | None:
    try:
        request = ctx.request_context.request  # type: ignore[union-attr]
        if request is None:
            return None
        v = request.headers.get(name)
        return v.strip() if v else None
    except Exception:  # noqa: BLE001 - stdio has no request
        return None


@dataclass(frozen=True)
class Defaults:
    ledger: str
    language: str
    controlling_area: str | None


def get_defaults(ctx: Context | None = None) -> Defaults:
    return Defaults(
        ledger=(_header(ctx, HEADER_PREFIX + "ledger") or config.get_default_ledger()).strip(),
        language=(_header(ctx, HEADER_PREFIX + "language") or config.get_language()).strip().upper(),
        controlling_area=(_header(ctx, HEADER_PREFIX + "controlling-area") or os.getenv("S4HC_CONTROLLING_AREA") or "").strip() or None,
    )


def get_client(ctx: Context | None = None) -> S4HCClient:
    base_url = config.get_base_url(_header(ctx, HEADER_PREFIX + "base-url"))
    header_user = _header(ctx, HEADER_PREFIX + "user")
    user = header_user or os.getenv("S4HC_USER")
    password = _header(ctx, HEADER_PREFIX + "password") or os.getenv("S4HC_PASSWORD")
    cert = None if header_user else config.resolve_client_cert()

    key = f"{base_url}|{user or ''}|{'cert' if cert else 'basic'}"
    client = _cached.get(key)
    if client is None or (password and client.session.auth != (user, password)):
        client = S4HCClient(base_url, user=user, password=password, cert=cert)
        _cached[key] = client
    return client


def cached(ctx: Context | None, key: str, loader: Callable[[], Any]) -> Any:
    """Small TTL cache for master data, partitioned by tenant base URL, the
    communication user AND the effective data scope, so a scoped connection
    can never be served another connection's result."""
    from .governance import get_scope

    try:
        tenant = config.get_base_url(_header(ctx, HEADER_PREFIX + "base-url"))
    except RuntimeError:
        tenant = "unset"
    user = _header(ctx, HEADER_PREFIX + "user") or os.getenv("S4HC_USER") or ""
    sc = get_scope(ctx)
    k = f"{tenant}|{user}|{sorted(sc.company_codes)}|{sorted(sc.cost_centers)}|{key}"
    hit = _master_cache.get(k)
    now = time.time()
    if hit and now - hit[0] < MASTER_TTL_SECONDS:
        return hit[1]
    value = loader()
    _master_cache[k] = (now, value)
    return value


def connection_info(ctx: Context | None = None) -> dict[str, Any]:
    """Non-secret summary of how the connection is configured."""
    try:
        base_url = config.get_base_url(_header(ctx, HEADER_PREFIX + "base-url"))
    except RuntimeError as exc:
        base_url = f"<unset: {exc}>"
    d = get_defaults(ctx)
    return {
        "base_url": base_url,
        "auth_mode": "header-basic"
        if _header(ctx, HEADER_PREFIX + "user")
        else ("x509" if config.resolve_client_cert() else ("basic" if os.getenv("S4HC_USER") else "none")),
        "default_ledger": d.ledger,
        "language": d.language,
        "controlling_area": d.controlling_area,
        "page_size": config.get_page_size(),
        "max_rows": config.get_max_rows(),
    }
