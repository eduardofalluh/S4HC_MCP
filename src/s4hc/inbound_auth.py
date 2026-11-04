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

import anyio
import requests
from fastmcp.server.auth import AccessToken, RemoteAuthProvider, TokenVerifier
from fastmcp.server.auth.providers.jwt import JWTVerifier
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import config
from .client import HeaderAuth
from .errors import S4HCError
from .governance import _TokenBucket

CLOCK_SKEW_SECONDS = 60
SAP_TOKEN_CHECK_TTL = 300
SAP_TOKEN_REFUSED_TTL = 60
SAP_TOKEN_CHECK_CONCURRENCY = 8
MAX_CHECKED_SAP_TOKENS = 4096
_KEY = secrets.token_bytes(32)

log = logging.getLogger("s4hc-mcp.inbound-auth")


def _csv(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").replace(" ", ",").split(",") if v.strip()]


def sap_auth_mode() -> str:
    return (os.getenv("S4HC_SAP_AUTH") or "").strip().lower()


def oidc_enabled() -> bool:
    return bool(os.getenv("S4HC_OIDC_ISSUER"))


def public_url() -> str:
    url = (os.getenv("S4HC_PUBLIC_URL") or "").strip().rstrip("/")
    if not url.startswith(("https://", "http://127.0.0.1", "http://localhost")):
        raise S4HCError("S4HC_PUBLIC_URL must be this server's public https URL (used in OAuth metadata)",
                        "NOT_CONFIGURED")
    return url


class StrictJWTVerifier(JWTVerifier):
    """FastMCP's JWTVerifier accepts a token without ``exp`` (it never expires) and
    does not check ``nbf``. Both are required here."""

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


class SapUserTokenVerifier(TokenVerifier):
    """S/4HANA access tokens are opaque, so the only authority that can check one is
    S/4HANA itself: a $metadata read with the token must succeed. Results are cached
    per token (keyed by HMAC, never the token itself): accepted ones for
    SAP_TOKEN_CHECK_TTL, refused ones for SAP_TOKEN_REFUSED_TTL. Checks run before
    any other limit applies (the caller is not authenticated yet), so checks of
    tokens SAP has never accepted are bounded: SAP_TOKEN_CHECK_CONCURRENCY at a time
    and S4HC_SAP_TOKEN_CHECKS_PER_MIN across all callers; over that a new token is
    refused (401) without asking SAP. A token SAP accepted before is re-checked
    outside that budget, which invented tokens can drain: a flood can delay new
    sign-ins but never log out a signed-in user. There is no per-client bound: the
    peer address is the ingress or GenAI Studio's proxy, and X-Forwarded-For is set
    by the caller."""

    def __init__(self) -> None:
        super().__init__()
        self._ok: OrderedDict[str, float] = OrderedDict()
        self._refused: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()
        self._checks = _TokenBucket(int(os.getenv("S4HC_SAP_TOKEN_CHECKS_PER_MIN", "60")))
        self._in_flight = threading.BoundedSemaphore(SAP_TOKEN_CHECK_CONCURRENCY)

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

    def _remember(self, cache: OrderedDict[str, float], k: str, until: float) -> None:
        with self._lock:
            cache[k] = until
            cache.move_to_end(k)
            while len(cache) > MAX_CHECKED_SAP_TOKENS:
                cache.popitem(last=False)

    async def verify_token(self, token: str) -> AccessToken | None:
        if not token or len(token) > 8192:
            return None
        k = hmac.new(_KEY, token.encode(), hashlib.sha256).hexdigest()
        now = time.time()
        with self._lock:
            ok_until, refused_until = self._ok.get(k), self._refused.get(k)
        if refused_until and refused_until > now:
            return None
        if ok_until is None:  # never accepted: bounded, shared with every unknown caller
            if not self._in_flight.acquire(blocking=False):
                log.warning("S/4HANA token check refused without asking SAP: too many checks in flight")
                return None
            try:
                if not self._checks.allow():
                    log.warning("S/4HANA token check refused without asking SAP: per-minute budget used up")
                    return None
                ok = await anyio.to_thread.run_sync(self._check, token)
            finally:
                self._in_flight.release()
        elif ok_until < now:  # accepted before: re-checked outside the budget strangers can drain
            ok = await anyio.to_thread.run_sync(self._check, token)
        else:
            ok = True
        if ok is False:
            with self._lock:
                self._ok.pop(k, None)
            self._remember(self._refused, k, now + SAP_TOKEN_REFUSED_TTL)
        if not ok:
            return None
        if ok_until is None or ok_until < now:
            self._remember(self._ok, k, now + SAP_TOKEN_CHECK_TTL)
        # A per-token subject, so the MCP SDK binds each session to the user who opened it.
        return AccessToken(token=token, client_id="s4hana-oauth", scopes=[], expires_at=None,
                           subject="sap-token:" + k[:32], claims={"s4hc_sap_user_token": True})


class SapOAuthProvider(RemoteAuthProvider):
    """RFC 9728 metadata plus an RFC 8414 document describing S/4HANA's own OAuth
    endpoints, which S/4HANA does not publish itself (checked on a 2026 tenant)."""

    def get_routes(self, mcp_path: str | None = None) -> list[Route]:
        routes = super().get_routes(mcp_path)
        api = config.get_base_url(None)
        authorize = os.getenv("S4HC_SAP_OAUTH_AUTHORIZE_URL") or f"{api.replace('-api.', '.', 1)}/sap/bc/sec/oauth2/authorize"
        token = os.getenv("S4HC_OAUTH_TOKEN_URL") or f"{api}/sap/bc/sec/oauth2/token"
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
    return RemoteAuthProvider(token_verifier=ApiKeyOrVerifier(verifier, api_key), authorization_servers=servers,
                              base_url=base, scopes_supported=_csv(os.getenv("S4HC_OIDC_SCOPES")) or None,
                              resource_name="SAP S/4HANA Cloud MCP")
