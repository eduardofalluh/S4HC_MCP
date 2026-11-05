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

import base64
import hashlib
import hmac
import os
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable

from fastmcp import Context

from . import config
from .client import S4HCClient
from .errors import S4HCError

MASTER_TTL_SECONDS = int(os.getenv("S4HC_MASTER_CACHE_TTL", "600"))
MAX_CLIENTS = int(os.getenv("S4HC_MAX_CACHED_CONNECTIONS", "64"))
MAX_MASTER_ENTRIES = int(os.getenv("S4HC_MAX_CACHED_RESULTS", "512"))

MAX_VERIFIED = 4096

_cached: OrderedDict[str, S4HCClient] = OrderedDict()
_master_cache: OrderedDict[str, tuple[float, Any]] = OrderedDict()
_verified: OrderedDict[str, None] = OrderedDict()  # fingerprints SAP has answered 2xx for
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
    source: str  # header | env | oauth2 | destination | principal_propagation | user_token
    identity: str = ""  # non-secret identity, for sources whose credential is a token
    headers: Callable[[], dict[str, str]] | None = field(default=None, compare=False, repr=False)
    target: Callable[[], Any] | None = field(default=None, compare=False, repr=False)

    @property
    def per_call(self) -> bool:
        """Credentials bound to one signed-in user's current token: never cached."""
        return self.source in ("principal_propagation", "user_token")

    @property
    def fingerprint(self) -> str:
        """Stable, non-reversible identity of tenant + credential. Two callers
        share a cached client or cached result only if this matches."""
        if self.identity:
            raw = "\0".join([self.source, self.base_url, self.identity])
        else:
            secret = self.password if self.password is not None else repr(self.cert)
            raw = "\0".join([self.base_url, self.user or "", secret or ""])
        return hmac.new(_FINGERPRINT_KEY, raw.encode(), hashlib.sha256).hexdigest()[:32]


def _access_token(ctx: Context | None):
    """The caller's verified token (HTTP with an auth provider), or None."""
    try:
        from fastmcp.server.dependencies import get_access_token
        return get_access_token()
    except Exception:  # noqa: BLE001 - stdio / no auth provider
        return None


def _signed_in_subject(ctx: Context | None) -> str | None:
    """issuer + subject of a verified user token (OIDC), or None (API key, SAP token, stdio)."""
    claims = getattr(_access_token(ctx), "claims", None) or {}
    if claims.get("sub") and not claims.get("s4hc_api_key"):
        return f"{claims.get('iss', '')}|{claims['sub']}"
    return None


def _has_signed_in_user(ctx: Context | None) -> bool:
    """A verified user token (OIDC subject or S/4HANA token), as opposed to the API key or stdio."""
    claims = getattr(_access_token(ctx), "claims", None) or {}
    return bool(claims.get("s4hc_sap_user_token") or _signed_in_subject(ctx))


def caller_identity(ctx: Context | None) -> str | None:
    """Audit identity of a signed-in caller: the token's subject, never a header."""
    tok = _access_token(ctx)
    claims = getattr(tok, "claims", None) or {}
    if claims.get("sub") and not claims.get("s4hc_api_key"):
        return "sub:" + str(claims["sub"])[:64]
    if claims.get("s4hc_sap_user_token"):
        return "sap-token:" + hmac.new(_FINGERPRINT_KEY, str(tok.token).encode(), hashlib.sha256).hexdigest()[:12]
    if claims.get("s4hc_api_key"):
        return "api-key"
    return None


def _user_connection(ctx: Context | None, mode: str) -> Connection:
    """Principal propagation / SAP user token: the SAP identity is the signed-in user's,
    so header credentials, a header base URL or an API-key caller are refused.
    Server configuration is checked first, so UNAUTHORIZED_SCOPE always means
    "configured, but this call has no usable user"."""
    url = config.get_base_url(None) if mode == "user_token" else ""
    dest = _destination_name() if mode == "principal_propagation" else ""
    if _header(ctx, HEADER_PREFIX + "user") or _header(ctx, HEADER_PREFIX + "password") \
            or _header(ctx, HEADER_PREFIX + "base-url"):
        raise S4HCError(f"S4HC_SAP_AUTH={mode}: the SAP identity comes from the user's sign-in; "
                        "x-s4hc-base-url / x-s4hc-user / x-s4hc-password are not accepted", "UNAUTHORIZED_SCOPE")
    tok = _access_token(ctx)
    claims = getattr(tok, "claims", None) or {}
    if mode == "user_token":
        if not claims.get("s4hc_sap_user_token"):
            raise S4HCError("This deployment runs every call as the signed-in S/4HANA user; sign in through the "
                            "MCP client's OAuth flow (an API key carries no SAP user)", "UNAUTHORIZED_SCOPE")
        token = str(tok.token)
        return Connection(url, None, None, None, "user_token",
                          identity=hashlib.sha256(token.encode()).hexdigest(),
                          headers=lambda: {"Authorization": f"Bearer {token}"})
    if not claims.get("sub") or claims.get("s4hc_api_key"):
        raise S4HCError("Principal propagation runs every call as the signed-in user; this call has no user token "
                        "(API keys and stdio carry no user). Nothing falls back to a shared SAP identity.",
                        "UNAUTHORIZED_SCOPE")
    user_jwt = str(tok.token)
    from . import sap_auth
    return Connection("", None, None, None, "principal_propagation",
                      identity=f"{dest}|{claims.get('iss', '')}|{claims['sub']}",
                      target=lambda: sap_auth.destination_service().resolve(dest, user_jwt=user_jwt))


def _destination_name() -> str:
    name = (os.getenv("S4HC_BTP_DESTINATION") or "").strip()
    if not name:
        raise S4HCError("S4HC_BTP_DESTINATION (the BTP destination name) is required for this S4HC_SAP_AUTH mode",
                        "NOT_CONFIGURED")
    return name


def resolve_connection(ctx: Context | None = None) -> Connection:
    """Who this call reaches SAP as. Cheap: no network. Token-based credentials are
    fetched lazily (Connection.headers / Connection.target) when a client is built."""
    from .inbound_auth import mixed_auth, sap_auth_mode

    mode = sap_auth_mode()
    if mode in ("principal_propagation", "user_token"):
        if not (mixed_auth() and not _has_signed_in_user(ctx)):
            return _user_connection(ctx, mode)
        # Mixed mode, and no signed-in user (API key, stdio): the previous setup below.
        # Decided by how the caller signed in, so a signed-in user never lands here.
        mode = ""

    h_url = _header(ctx, HEADER_PREFIX + "base-url")
    h_user = _header(ctx, HEADER_PREFIX + "user")
    h_password = _header(ctx, HEADER_PREFIX + "password")

    if h_user or h_password:
        if not (h_user and h_password):
            raise S4HCError("x-s4hc-user and x-s4hc-password must be sent together", "NOT_CONFIGURED")
        return Connection(config.get_base_url(h_url), h_user, h_password, None, "header")

    if mode == "destination":
        if h_url:
            raise S4HCError("S4HC_SAP_AUTH=destination: the SAP host comes from the BTP destination, "
                            "x-s4hc-base-url is not accepted", "NOT_CONFIGURED")
        dest = _destination_name()
        from . import sap_auth
        return Connection("", None, None, None, "destination", identity=dest,
                          target=lambda: sap_auth.destination_service().resolve(dest))

    env_url = config.get_base_url(None) if os.getenv("S4HC_BASE_URL") else None
    if h_url:
        url = config.get_base_url(h_url)
        if url != env_url:
            raise S4HCError("x-s4hc-base-url names a host other than this server's S4HC_BASE_URL, so it must "
                            "come with x-s4hc-user and x-s4hc-password; server credentials are never sent "
                            "to a caller-chosen host", "NOT_CONFIGURED")
    url = config.get_base_url(None)
    if mode == "oauth2_client_credentials":
        from . import sap_auth
        cc = sap_auth.client_credentials_token(url)
        return Connection(url, None, None, None, "oauth2", identity=f"{cc.token_url}|{cc.client_id}",
                          headers=cc.headers)
    if mode not in ("", "basic", "x509"):
        raise S4HCError(f"Unknown S4HC_SAP_AUTH {mode!r}: use basic, x509, oauth2_client_credentials, destination, "
                        "principal_propagation or user_token", "NOT_CONFIGURED")
    # Unset: certificate if one is configured, else the password. An explicit mode is enforced.
    cert = None if mode == "basic" else config.resolve_client_cert()
    if mode == "x509" and not cert:
        raise S4HCError("S4HC_SAP_AUTH=x509 needs S4HC_CLIENT_CERT + S4HC_CLIENT_KEY (or S4HC_CLIENT_CERT_PEM)",
                        "NOT_CONFIGURED")
    user, password = os.getenv("S4HC_USER"), os.getenv("S4HC_PASSWORD")
    if cert:
        return Connection(url, user, None, cert, "env")
    return Connection(url, user, password, None, "env")


def _target_headers(resolver: Callable[[], Any], base_url: str, proxy_url: str | None) -> Callable[[], dict[str, str]]:
    """Credentials of the current destination lookup, on every request, so a rotated
    password or token is picked up. A destination that now points at another host or
    proxy fails the request instead of sending its credential to the old one."""
    def headers() -> dict[str, str]:
        t = resolver()
        if (t.base_url, t.proxy_url) != (base_url, proxy_url):
            raise S4HCError("The BTP destination's URL or proxy changed during this call; retry", "SAP_UNAVAILABLE")
        h = dict(t.headers)
        if t.basic and "Authorization" not in h:
            h["Authorization"] = "Basic " + base64.b64encode(f"{t.basic[0]}:{t.basic[1]}".encode()).decode()
        if t.sap_client:
            h["sap-client"] = t.sap_client
        return h
    return headers


def build_client(conn: Connection, *, timeout: int = 60, retries: int = 3,
                 on_verified: Callable[[], None] | None = None) -> S4HCClient:
    if conn.target is not None:
        first = conn.target()
        return S4HCClient(first.base_url, auth_headers=_target_headers(conn.target, first.base_url, first.proxy_url),
                          proxy_url=first.proxy_url, timeout=timeout, retries=retries, on_verified=on_verified)
    return S4HCClient(conn.base_url, user=conn.user, password=conn.password, cert=conn.cert,
                      auth_headers=conn.headers, timeout=timeout, retries=retries, on_verified=on_verified)


def _route(conn: Connection) -> str:
    """Where a destination-based connection points right now (host, proxy, sap-client),
    for cache keys: a re-pointed destination gets its own client and its own cached
    master data. Empty for other connections. Not network-free (a cached lookup), so
    it is never called from resolve_connection()."""
    if conn.target is None:
        return ""
    t = conn.target()
    return f"|{t.base_url}|{t.proxy_url or ''}|{t.sap_client or ''}"


def get_client(ctx: Context | None = None) -> S4HCClient:
    conn = resolve_connection(ctx)
    fp = conn.fingerprint
    if conn.per_call:  # bound to this request's user token: never reused for another call
        return build_client(conn, on_verified=lambda: mark_verified(fp))
    key = fp + _route(conn)
    with _lock:
        client = _cached.get(key)
        if client is not None:
            _cached.move_to_end(key)
            return client
    client = build_client(conn, on_verified=lambda: mark_verified(fp))
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


def mark_verified(fingerprint: str) -> None:
    """Only a 2xx from SAP calls this, so the set holds credentials SAP accepted."""
    with _lock:
        _verified[fingerprint] = None
        _verified.move_to_end(fingerprint)
        while len(_verified) > MAX_VERIFIED:
            _verified.popitem(last=False)


def rate_limit_key(ctx: Context | None = None) -> tuple[str, bool]:
    """(bucket key, trusted). The server's own env connection and any credential SAP
    has already accepted are trusted; everything else is an unverified caller,
    limited separately so it cannot crowd out trusted ones. A signed-in (OIDC) user
    gets a bucket of their own even on a shared SAP identity, so one user cannot use
    up the budget of everyone else on that identity."""
    try:
        conn = resolve_connection(ctx)
    except S4HCError:
        return "unresolved", False
    key = conn.fingerprint
    subject = _signed_in_subject(ctx)
    if subject:
        key = hmac.new(_FINGERPRINT_KEY, f"{key}\0{subject}".encode(), hashlib.sha256).hexdigest()[:32]
    # Server-configured identities, and users whose token this server verified itself.
    if conn.source in ("env", "oauth2", "destination", "principal_propagation"):
        return key, True
    with _lock:
        return key, conn.fingerprint in _verified


def cached(ctx: Context | None, key: str, loader: Callable[[], Any]) -> Any:
    """Small TTL cache for master data, partitioned by tenant + credential
    fingerprint AND the effective data scope, so a connection is never served
    a result fetched with another connection's credentials or scope."""
    from .governance import get_scope

    sc = get_scope(ctx)
    conn = resolve_connection(ctx)
    k = f"{conn.fingerprint}{_route(conn)}|{sorted(sc.company_codes)}|{sorted(sc.cost_centers)}|{key}"
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
    """Non-secret summary of how the connection is configured. auth_mode is how this
    caller reaches SAP; signed_in_users is how signed-in users do, in a per-user mode."""
    from .inbound_auth import mixed_auth, oidc_enabled, sap_auth_mode

    modes = {"header": "header-basic", "oauth2": "oauth2-client-credentials", "destination": "btp-destination",
             "principal_propagation": "principal-propagation", "user_token": "sap-user-token"}
    try:
        conn = resolve_connection(ctx)
        base_url = conn.base_url or f"<from BTP destination {_destination_name()}>"
        if conn.source in modes:
            auth_mode = modes[conn.source]
        elif conn.cert:
            auth_mode = "x509"
        else:
            auth_mode = "basic" if conn.user and conn.password else "none"
    except S4HCError as exc:
        base_url, auth_mode = f"<unset: {exc}>", "none"
        mode = sap_auth_mode()
        # Per-user modes check their configuration first, so UNAUTHORIZED_SCOPE here only
        # means no signed-in user on this call (startup, stdio): configured, not broken.
        if exc.code == "UNAUTHORIZED_SCOPE" and mode in ("principal_propagation", "user_token"):
            auth_mode = modes[mode]
            base_url = (f"<per signed-in user, BTP destination {_destination_name()}>"
                        if mode == "principal_propagation" else config.get_base_url(None))
    d = get_defaults(ctx)
    mcp_auth = ("sap-oauth" if sap_auth_mode() == "user_token" else "oidc" if oidc_enabled()
                else "api-key" if os.getenv("S4HC_MCP_API_KEY") else "none")
    per_user = sap_auth_mode() if sap_auth_mode() in ("principal_propagation", "user_token") else None
    return {
        "base_url": base_url,
        "auth_mode": auth_mode,
        "mcp_auth": mcp_auth,
        "signed_in_users": modes[per_user] if per_user else None,
        "mixed_auth": bool(per_user) and mixed_auth(),
        "default_ledger": d.ledger,
        "language": d.language,
        "controlling_area": d.controlling_area,
        "page_size": config.get_page_size(),
        "max_rows": config.get_max_rows(),
    }
