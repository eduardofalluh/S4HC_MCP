"""Inbound authentication: who is calling this MCP server (AI client -> MCP).

Modes (HTTP transport only; stdio is a local, single-user process):

* none / API key (default): handled by server.py's _InboundKeyMiddleware.
* OIDC resource server (``S4HC_OIDC_ISSUER``): every call carries a JWT from
  the operator's identity provider (SAP BTP XSUAA, SAP Cloud Identity Services,
  Microsoft Entra ID, ...). Signature, issuer, audience, expiry and not-before
  are checked locally against the provider's keys. RFC 9728 metadata tells MCP
  clients where to sign in. S4HC_MCP_API_KEY, if set, is still accepted for
  automation, but it never carries a user, so it cannot be used where the SAP
  call needs one (principal propagation).
* SAP OAuth (``S4HC_SAP_AUTH=user_token``): the same pattern CALM_MCP uses with
  SAP Cloud ALM. The MCP client signs the user in at S/4HANA's own OAuth server
  (authorization code grant) and sends that S/4HANA token; this server checks
  it with S/4HANA and forwards it, so every OData call runs as that user.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import threading
import time
from collections import OrderedDict
from typing import Any

import anyio
import requests
from fastmcp.server.auth import AccessToken, RemoteAuthProvider, TokenVerifier
from fastmcp.server.auth.providers.jwt import JWTVerifier
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import config
from .client import HeaderAuth
from .errors import S4HCError
from .governance import _TokenBucket
from .sap_auth import _https

CLOCK_SKEW_SECONDS = 60
SAP_TOKEN_CHECK_TTL = 300
SAP_TOKEN_MAX_UNCONFIRMED = 600
SAP_TOKEN_RETRY_SECONDS = 30
SAP_TOKEN_REFUSED_TTL = 60
SAP_TOKEN_CHECK_CONCURRENCY = 8
MAX_CHECKED_SAP_TOKENS = 4096
MAX_TOKEN_CHECK_SOURCES = 1024
_KEY = secrets.token_bytes(32)

log = logging.getLogger("s4hc-mcp.inbound-auth")


def _csv(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").replace(" ", ",").split(",") if v.strip()]


def sap_auth_mode() -> str:
    return (os.getenv("S4HC_SAP_AUTH") or "").strip().lower()


def oidc_enabled() -> bool:
    return bool(os.getenv("S4HC_OIDC_ISSUER"))


def mixed_auth() -> bool:
    """S4HC_MIXED_AUTH=true (ARC-1: SAP_PP_STRICT=false): in a per-user mode, callers
    without a signed-in user (API key, stdio) use the previous setup, the communication
    user or x-s4hc-* headers, instead of being refused. Signed-in users still always run
    as themselves: a failed per-user call is refused, never retried as the shared user."""
    return (os.getenv("S4HC_MIXED_AUTH") or "").strip().lower() in ("1", "true", "yes")


def public_url() -> str:
    url = (os.getenv("S4HC_PUBLIC_URL") or "").strip().rstrip("/")
    if not url.startswith(("https://", "http://127.0.0.1", "http://localhost")):
        raise S4HCError("S4HC_PUBLIC_URL must be this server's public https URL (used in OAuth metadata)",
                        "NOT_CONFIGURED")
    return url


class StrictJWTVerifier(JWTVerifier):
    """FastMCP's JWTVerifier accepts a token without ``exp`` (it never expires) and
    does not check ``nbf``. Both are required here. Claims named ``s4hc_*`` are this
    server's own markers (an API-key caller, an S/4HANA token); a user token that
    carries one is refused, so an identity provider's claims can never make a
    signed-in user look like the API key and reach the communication user."""

    async def load_access_token(self, token: str) -> AccessToken | None:
        verified = await super().load_access_token(token)
        if verified is None:
            return None
        claims = verified.claims or {}
        now = time.time()
        if not isinstance(claims.get("exp"), (int, float)):
            return None
        nbf = claims.get("nbf")
        if isinstance(nbf, (int, float)) and nbf > now + CLOCK_SKEW_SECONDS:
            return None
        if not claims.get("sub"):
            return None
        if any(str(name).startswith("s4hc_") for name in claims):
            log.warning("Sign-in token refused: it carries a claim reserved for this server (s4hc_*)")
            return None
        return verified


class ApiKeyOrVerifier(TokenVerifier):
    """Accepts S4HC_MCP_API_KEY (constant-time) or whatever the wrapped verifier
    accepts. An API-key caller gets claims {"s4hc_api_key": True} and no user."""

    def __init__(self, inner: TokenVerifier, api_key: str | None) -> None:
        super().__init__(base_url=inner.base_url, required_scopes=inner.required_scopes)
        self.inner = inner
        self._api_key = api_key.encode() if api_key else None

    async def verify_token(self, token: str) -> AccessToken | None:
        if self._api_key and hmac.compare_digest(token.encode(), self._api_key):
            return AccessToken(token="api-key", client_id="api-key", scopes=list(self.required_scopes or []),
                               expires_at=None, claims={"s4hc_api_key": True})
        return await self.inner.verify_token(token)


def _source_of(peer: str, forwarded_for: str, trusted_hops: int) -> str:
    """The request's sender, as far as it can be trusted. With N trusted proxies in
    front, the last N X-Forwarded-For entries were appended by them, and the entry the
    outermost one appended is the address it saw; entries left of it are set by the
    caller. Without trusted proxies (or a shorter chain) it is the peer address."""
    if trusted_hops > 0:
        chain = [p.strip() for p in forwarded_for.split(",") if p.strip()]
        if len(chain) >= trusted_hops:
            return _address_only(chain[-trusted_hops])
    return peer


def _address_only(entry: str) -> str:
    """Some proxies append ``ip:port``; a port that changes per connection must not
    turn one sender into many."""
    if entry.startswith("[") and "]" in entry:  # [IPv6]:port
        return entry[1:entry.index("]")]
    if entry.count(":") == 1:  # IPv4:port
        return entry.split(":", 1)[0]
    return entry


def _request_source(trusted_hops: int) -> str:
    try:
        from fastmcp.server.dependencies import get_http_request
        request = get_http_request()  # set by FastMCP's outermost middleware, so available during auth
    except Exception:  # noqa: BLE001 - no HTTP request (stdio, unit tests)
        return "-"
    peer = request.client.host if request.client else "-"
    # Every header line, in order: a proxy may add its own line instead of extending the caller's.
    return _source_of(peer, ", ".join(request.headers.getlist("x-forwarded-for")), trusted_hops)


class SapUserTokenVerifier(TokenVerifier):
    """S/4HANA access tokens are opaque, so the only authority that can check one is
    S/4HANA itself: a $metadata read with the token must succeed. Results are cached
    per token (keyed by HMAC, never the token itself). Checks run before the caller
    is authenticated, so every path to SAP is bounded:

    * A token SAP has never accepted: SAP_TOKEN_CHECK_CONCURRENCY at a time, at most
      S4HC_SAP_TOKEN_CHECKS_PER_SOURCE_PER_MIN per sender (see _source_of and
      S4HC_TRUSTED_PROXY_HOPS) and S4HC_SAP_TOKEN_CHECKS_PER_MIN in all; over that it
      is refused (401) without asking SAP. One sender cannot use up the budget for
      the others; a flood from many senders can still delay sign-ins and token
      refreshes (a refreshed token is a new token).
    * A token SAP accepted: trusted for SAP_TOKEN_CHECK_TTL, then re-checked outside
      that budget, one check per token at a time and SAP_TOKEN_CHECK_CONCURRENCY in
      all. While a re-check is running or cannot run, or SAP gives no answer (retried
      after SAP_TOKEN_RETRY_SECONDS), the token stays accepted until
      SAP_TOKEN_MAX_UNCONFIRMED after SAP last confirmed it. Only SAP refusing it
      revokes it.

    Token checks therefore never hold more than 2 x SAP_TOKEN_CHECK_CONCURRENCY of
    the worker threads that tool calls run on."""

    def __init__(self) -> None:
        super().__init__()
        self._ok: OrderedDict[str, tuple[float, float]] = OrderedDict()  # (confirmed_at, retry_at)
        self._refused: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()
        self._checks = _TokenBucket(int(os.getenv("S4HC_SAP_TOKEN_CHECKS_PER_MIN", "60")))
        self._per_source = int(os.getenv("S4HC_SAP_TOKEN_CHECKS_PER_SOURCE_PER_MIN", "20"))
        self._trusted_hops = int(os.getenv("S4HC_TRUSTED_PROXY_HOPS", "0"))
        self._sources: OrderedDict[str, _TokenBucket] = OrderedDict()
        self._in_flight = threading.BoundedSemaphore(SAP_TOKEN_CHECK_CONCURRENCY)
        self._rechecks = threading.BoundedSemaphore(SAP_TOKEN_CHECK_CONCURRENCY)
        self._rechecking: set[str] = set()

    def _check(self, token: str) -> bool | None:
        """True: SAP accepted the token. False: SAP refused it. None: no answer."""
        base = config.get_base_url(None)
        try:
            r = requests.get(f"{base}{config.JOURNAL_ITEM_SERVICE}/$metadata", timeout=15, allow_redirects=False,
                             headers={"Accept": "application/xml"}, stream=True,
                             auth=HeaderAuth(lambda: {"Authorization": f"Bearer {token}"}))  # never ~/.netrc's
            r.close()
        except requests.RequestException:
            return None
        if r.status_code == 200:
            return True
        return False if r.status_code in (401, 403) else None

    def _remember(self, cache: OrderedDict, k: str, value: Any) -> None:
        with self._lock:
            cache[k] = value
            cache.move_to_end(k)
            while len(cache) > MAX_CHECKED_SAP_TOKENS:
                cache.popitem(last=False)

    def _source_bucket(self, source: str) -> _TokenBucket:
        with self._lock:
            b = self._sources.get(source)
            if b is None:
                b = self._sources[source] = _TokenBucket(self._per_source)
                while len(self._sources) > MAX_TOKEN_CHECK_SOURCES:
                    self._sources.popitem(last=False)
            else:
                self._sources.move_to_end(source)
            return b

    async def _first_check(self, token: str) -> bool | None:
        source = _request_source(self._trusted_hops)
        if not self._in_flight.acquire(blocking=False):
            log.warning("S/4HANA token check refused without asking SAP: too many checks in flight")
            return None
        try:
            # The sender's own share first, so a sender over it does not use up the shared budget.
            if not self._source_bucket(source).allow() or not self._checks.allow():
                log.warning("S/4HANA token check refused without asking SAP: per-minute budget used up")
                return None
            return await anyio.to_thread.run_sync(self._check, token)
        finally:
            self._in_flight.release()

    async def _recheck(self, token: str, k: str, retry_at: float, now: float) -> bool | None:
        """None when no check ran (one is running, no slot, back-off) or SAP gave no answer."""
        with self._lock:
            if k in self._rechecking or now < retry_at or not self._rechecks.acquire(blocking=False):
                return None
            self._rechecking.add(k)
        try:
            ok = await anyio.to_thread.run_sync(self._check, token)
        finally:
            with self._lock:
                self._rechecking.discard(k)
            self._rechecks.release()
        if ok is None:
            with self._lock:
                if k in self._ok:
                    self._ok[k] = (self._ok[k][0], time.time() + SAP_TOKEN_RETRY_SECONDS)
        return ok

    def _accepted(self, token: str, k: str) -> AccessToken:
        # A per-token subject, so the MCP SDK binds each session to the user who opened it.
        return AccessToken(token=token, client_id="s4hana-oauth", scopes=[], expires_at=None,
                           subject="sap-token:" + k[:32], claims={"s4hc_sap_user_token": True})

    async def verify_token(self, token: str) -> AccessToken | None:
        if not token or len(token) > 8192:
            return None
        k = hmac.new(_KEY, token.encode(), hashlib.sha256).hexdigest()
        now = time.time()
        with self._lock:
            entry, refused_until = self._ok.get(k), self._refused.get(k)
        if refused_until and refused_until > now:
            return None
        if entry is None:
            ok = await self._first_check(token)
        else:
            confirmed_at, retry_at = entry
            if now - confirmed_at < SAP_TOKEN_CHECK_TTL:
                return self._accepted(token, k)
            ok = await self._recheck(token, k, retry_at, now)
            if ok is None:
                return self._accepted(token, k) if now - confirmed_at < SAP_TOKEN_MAX_UNCONFIRMED else None
        if ok is False:
            with self._lock:
                self._ok.pop(k, None)
            self._remember(self._refused, k, now + SAP_TOKEN_REFUSED_TTL)
        if not ok:
            return None
        self._remember(self._ok, k, (now, 0.0))
        return self._accepted(token, k)


class _ApiKeyHeaderAsBearer:
    """Clients set up for the API key alone may send it as ``x-api-key``. FastMCP's
    OAuth reads only Authorization, so such a request (one without Authorization) is
    passed on as ``Authorization: Bearer <key>``; ApiKeyOrVerifier then checks it."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            headers = scope.get("headers", [])
            names = {k.lower() for k, _ in headers}
            if b"authorization" not in names and b"x-api-key" in names:
                key = next(v for k, v in headers if k.lower() == b"x-api-key")
                scope = {**scope, "headers": [*headers, (b"authorization", b"Bearer " + key)]}
        await self.app(scope, receive, send)


class _AcceptsApiKeyHeader:
    def get_middleware(self) -> list:
        return [Middleware(_ApiKeyHeaderAsBearer), *super().get_middleware()]  # type: ignore[misc]


class OidcProvider(_AcceptsApiKeyHeader, RemoteAuthProvider):
    """RFC 9728 resource server for an external identity provider."""


def sap_oauth_endpoints() -> tuple[str, str]:
    """S/4HANA's authorize and token endpoints for the sign-in metadata. S/4HANA Cloud signs
    business users in through public OAuth clients (authorization code + PKCE, no secret), whose
    token endpoint is the /sap/public/ one (seen on a tenant, 2026-11)."""
    api = config.get_base_url(None)
    authorize = os.getenv("S4HC_SAP_OAUTH_AUTHORIZE_URL") or f"{api.replace('-api.', '.', 1)}/sap/bc/sec/oauth2/authorize"
    token = (os.getenv("S4HC_SAP_OAUTH_TOKEN_URL") or os.getenv("S4HC_OAUTH_TOKEN_URL")
             or f"{api}/sap/public/bc/sec/oauth2/token")
    return (_https(authorize, "S4HC_SAP_OAUTH_AUTHORIZE_URL"),
            _https(token, "S4HC_SAP_OAUTH_TOKEN_URL (or S4HC_OAUTH_TOKEN_URL)"))


class SapOAuthProvider(_AcceptsApiKeyHeader, RemoteAuthProvider):
    """RFC 9728 metadata plus an RFC 8414 document describing S/4HANA's own OAuth
    endpoints, which S/4HANA does not publish itself (checked on a 2026 tenant)."""

    def get_routes(self, mcp_path: str | None = None) -> list[Route]:
        routes = super().get_routes(mcp_path)
        authorize, token = sap_oauth_endpoints()
        meta = {
            "issuer": public_url(),
            "authorization_endpoint": authorize,
            "token_endpoint": token,
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"],
            "scopes_supported": _csv(os.getenv("S4HC_SAP_OAUTH_SCOPES")),
        }
        if (os.getenv("S4HC_SAP_OAUTH_PKCE", "true").lower() in ("1", "true", "yes")):
            meta["code_challenge_methods_supported"] = ["S256"]
            # A public client (no secret) is only safe with PKCE, so it is offered only with it.
            meta["token_endpoint_auth_methods_supported"].insert(0, "none")

        async def metadata(_: Request) -> JSONResponse:
            return JSONResponse(meta, headers={"Cache-Control": "public, max-age=3600"})

        routes.append(Route("/.well-known/oauth-authorization-server", metadata, methods=["GET"]))
        return routes


def build_auth_provider():
    """The FastMCP auth provider for this deployment, or None (API key / no auth)."""
    api_key = os.getenv("S4HC_MCP_API_KEY") or None
    if sap_auth_mode() == "user_token":
        if oidc_enabled():
            raise S4HCError("S4HC_SAP_AUTH=user_token signs users in at S/4HANA; it cannot be combined with "
                            "S4HC_OIDC_ISSUER", "NOT_CONFIGURED")
        base = public_url()
        sap_oauth_endpoints()  # refuse a bad sign-in URL at startup, not when a client first asks
        return SapOAuthProvider(token_verifier=ApiKeyOrVerifier(SapUserTokenVerifier(), api_key),
                                authorization_servers=[base], base_url=base,
                                scopes_supported=_csv(os.getenv("S4HC_SAP_OAUTH_SCOPES")),
                                resource_name="SAP S/4HANA Cloud MCP")
    if not oidc_enabled():
        if sap_auth_mode() == "principal_propagation":
            raise S4HCError("S4HC_SAP_AUTH=principal_propagation needs signed-in users: set S4HC_OIDC_ISSUER "
                            "(and S4HC_OIDC_AUDIENCE, S4HC_OIDC_JWKS_URI, S4HC_PUBLIC_URL)", "NOT_CONFIGURED")
        return None
    issuer = os.environ["S4HC_OIDC_ISSUER"].strip()
    audience = _csv(os.getenv("S4HC_OIDC_AUDIENCE"))
    if not audience:
        raise S4HCError("S4HC_OIDC_AUDIENCE is required: without an audience check a token issued for any other "
                        "application of the same identity provider would be accepted", "NOT_CONFIGURED")
    jwks_uri, public_key = os.getenv("S4HC_OIDC_JWKS_URI"), os.getenv("S4HC_OIDC_PUBLIC_KEY")
    if bool(jwks_uri) == bool(public_key):
        raise S4HCError("Set exactly one of S4HC_OIDC_JWKS_URI (normal) or S4HC_OIDC_PUBLIC_KEY (static key)",
                        "NOT_CONFIGURED")
    if jwks_uri and not jwks_uri.startswith("https://"):
        raise S4HCError("S4HC_OIDC_JWKS_URI must be https", "NOT_CONFIGURED")
    base = public_url()
    verifier = StrictJWTVerifier(jwks_uri=jwks_uri or None, public_key=public_key or None, issuer=issuer,
                                 audience=audience if len(audience) > 1 else audience[0],
                                 algorithm=os.getenv("S4HC_OIDC_ALGORITHM") or "RS256",
                                 required_scopes=_csv(os.getenv("S4HC_OIDC_REQUIRED_SCOPES")) or None,
                                 base_url=base)
    servers = _csv(os.getenv("S4HC_OIDC_AUTHORIZATION_SERVERS")) or [issuer]
    return OidcProvider(token_verifier=ApiKeyOrVerifier(verifier, api_key), authorization_servers=servers,
                        base_url=base, scopes_supported=_csv(os.getenv("S4HC_OIDC_SCOPES")) or None,
                        resource_name="SAP S/4HANA Cloud MCP")
