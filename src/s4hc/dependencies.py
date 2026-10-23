"""Resolve an S4HCClient and request-level defaults.

Resolution order (same idea as the CALM MCP):
  1. Per-request headers  x-s4hc-base-url / x-s4hc-user / x-s4hc-password
                          x-s4hc-ledger / x-s4hc-language / x-s4hc-controlling-area
     (multi-tenant GenAI Studio deployments inject these per connection)
  2. Environment          S4HC_BASE_URL / S4HC_USER / S4HC_PASSWORD or client cert
                          S4HC_LEDGER / S4HC_LANGUAGE / S4HC_CONTROLLING_AREA

Credentials are never mixed across the two sources: a connection either sends
user AND password in headers, or uses the server's own credentials — and the
server's credentials only ever go to the server's own S4HC_BASE_URL, never to a
host named in a request header.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable

from fastmcp import Context

from . import config
from .client import S4HCClient
from .errors import S4HCError

MASTER_TTL_SECONDS = int(os.getenv("S4HC_MASTER_CACHE_TTL", "600"))
MAX_CLIENTS = int(os.getenv("S4HC_MAX_CACHED_CONNECTIONS", "64"))
MAX_MASTER_ENTRIES = int(os.getenv("S4HC_MAX_CACHED_RESULTS", "512"))

_cached: OrderedDict[str, S4HCClient] = OrderedDict()
_master_cache: OrderedDict[str, tuple[float, Any]] = OrderedDict()
_lock = threading.Lock()

HEADER_PREFIX = "x-s4hc-"
# Keyed per process, so neither cache keys nor the prefixes in audit lines can be
# used to test password guesses offline.
_FINGERPRINT_KEY = secrets.token_bytes(32)


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


@dataclass(frozen=True)
class Connection:
    base_url: str
    user: str | None
    password: str | None
    cert: str | tuple[str, str] | None
    source: str  # "header" | "env"

    @property
    def fingerprint(self) -> str:
        """Stable, non-reversible identity of tenant + credential. Two callers
        share a cached client or cached result only if this matches."""
        secret = self.password if self.password is not None else repr(self.cert)
        raw = "\0".join([self.base_url, self.user or "", secret or ""])
        return hmac.new(_FINGERPRINT_KEY, raw.encode(), hashlib.sha256).hexdigest()[:32]


def resolve_connection(ctx: Context | None = None) -> Connection:
    h_url = _header(ctx, HEADER_PREFIX + "base-url")
    h_user = _header(ctx, HEADER_PREFIX + "user")
    h_password = _header(ctx, HEADER_PREFIX + "password")

    if h_user or h_password:
        if not (h_user and h_password):
            raise S4HCError("x-s4hc-user and x-s4hc-password must be sent together", "NOT_CONFIGURED")
        return Connection(config.get_base_url(h_url), h_user, h_password, None, "header")

    env_url = config.get_base_url(None) if os.getenv("S4HC_BASE_URL") else None
    if h_url:
        url = config.get_base_url(h_url)
        if url != env_url:
            raise S4HCError("x-s4hc-base-url names a host other than this server's S4HC_BASE_URL, so it must "
                            "come with x-s4hc-user and x-s4hc-password; server credentials are never sent "
                            "to a caller-chosen host", "NOT_CONFIGURED")
    url = config.get_base_url(None)
    cert = config.resolve_client_cert()
    user, password = os.getenv("S4HC_USER"), os.getenv("S4HC_PASSWORD")
    if cert:
        return Connection(url, user, None, cert, "env")
    return Connection(url, user, password, None, "env")


def get_client(ctx: Context | None = None) -> S4HCClient:
    conn = resolve_connection(ctx)
    key = conn.fingerprint
    with _lock:
        client = _cached.get(key)
        if client is not None:
            _cached.move_to_end(key)
            return client
    client = S4HCClient(conn.base_url, user=conn.user, password=conn.password, cert=conn.cert)
    with _lock:
        _cached[key] = client
        while len(_cached) > MAX_CLIENTS:
            _cached.popitem(last=False)
    return client


def connection_fingerprint(ctx: Context | None = None) -> str:
    try:
        return resolve_connection(ctx).fingerprint
    except S4HCError:
        return "unresolved"


def rate_limit_key(ctx: Context | None = None) -> tuple[str, bool]:
    """(bucket key, verified). A connection SAP has already accepted is keyed by
    its credential fingerprint; one that has not is keyed by host + user only,
    so cycling invented passwords cannot mint fresh buckets."""
    try:
        conn = resolve_connection(ctx)
    except S4HCError:
        return "unresolved", False
    with _lock:
        client = _cached.get(conn.fingerprint)
    if client is not None and client.verified:
        return conn.fingerprint, True
    return "unverified|" + hmac.new(_FINGERPRINT_KEY, f"{conn.base_url}\0{conn.user or ''}".encode(),
                                    hashlib.sha256).hexdigest()[:32], False


def cached(ctx: Context | None, key: str, loader: Callable[[], Any]) -> Any:
    """Small TTL cache for master data, partitioned by tenant + credential
    fingerprint AND the effective data scope, so a connection is never served
    a result fetched with another connection's credentials or scope."""
    from .governance import get_scope

    sc = get_scope(ctx)
    k = f"{resolve_connection(ctx).fingerprint}|{sorted(sc.company_codes)}|{sorted(sc.cost_centers)}|{key}"
    now = time.time()
    with _lock:
        hit = _master_cache.get(k)
        if hit and now - hit[0] < MASTER_TTL_SECONDS:
            _master_cache.move_to_end(k)
            return hit[1]
    value = loader()
    with _lock:
        _master_cache[k] = (now, value)
        _master_cache.move_to_end(k)
        while len(_master_cache) > MAX_MASTER_ENTRIES:
            _master_cache.popitem(last=False)
    return value


def connection_info(ctx: Context | None = None) -> dict[str, Any]:
    """Non-secret summary of how the connection is configured."""
    try:
        conn = resolve_connection(ctx)
        base_url = conn.base_url
        if conn.source == "header":
            auth_mode = "header-basic"
        elif conn.cert:
            auth_mode = "x509"
        else:
            auth_mode = "basic" if conn.user and conn.password else "none"
    except S4HCError as exc:
        base_url, auth_mode = f"<unset: {exc}>", "none"
    d = get_defaults(ctx)
    return {
        "base_url": base_url,
        "auth_mode": auth_mode,
        "default_ledger": d.ledger,
        "language": d.language,
        "controlling_area": d.controlling_area,
        "page_size": config.get_page_size(),
        "max_rows": config.get_max_rows(),
    }
