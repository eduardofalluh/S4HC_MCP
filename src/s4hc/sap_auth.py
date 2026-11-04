"""Outbound OAuth 2.0 to S/4HANA: how this server proves an identity to SAP.

Token sources, all configured by the operator (never by a caller):

* S/4HANA's own OAuth 2.0 server (``/sap/bc/sec/oauth2/token``) with the
  client-credentials grant: a short-lived token for a technical identity,
  instead of sending a communication user's password on every request.
* SAP BTP Destination service: the destination decides how SAP is reached.
  With a user token (``X-User-Token``) it propagates the end user, the way
  ARC-1 does it (github.com/arc-mcp/arc-1, ``applyPerUserAuthTokens``):
    - ``OAuth2SAMLBearerAssertion`` (S/4HANA Public Cloud, Internet): the
      service mints a SAML assertion for the user and exchanges it at
      S/4HANA's token endpoint, returning a Bearer token for that user.
    - ``PrincipalPropagation`` (S/4HANA Private Cloud / on-premise, OnPremise):
      the request goes through the BTP Connectivity proxy; the user's token is
      exchanged there (jwt-bearer) and Cloud Connector turns it into a
      short-lived X.509 certificate that SAP maps to the user (CERTRULE).
* The BTP Connectivity service, for OnPremise destinations.

Tokens and assertions are cached per destination and per user, never shared
across users, and never logged.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlsplit

import requests

from .errors import S4HCError

log = logging.getLogger("s4hc-mcp.sap-auth")

TOKEN_SKEW_SECONDS = 60
HTTP_TIMEOUT = 30
MAX_CACHED_USER_CREDENTIALS = 1024
_CACHE_KEY = secrets.token_bytes(32)  # per process: cache keys are not reversible to tokens


def _key(*parts: str) -> str:
    return hmac.new(_CACHE_KEY, "\0".join(parts).encode(), hashlib.sha256).hexdigest()


def _token_request(url: str, data: dict[str, str], client_id: str, client_secret: str, what: str) -> tuple[str, float]:
    """POST an OAuth token request; return (access_token, expires_at). Errors never echo secrets."""
    try:
        r = requests.post(url, data=data, auth=(client_id, client_secret), timeout=HTTP_TIMEOUT,
                          headers={"Accept": "application/json"}, allow_redirects=False)
    except requests.RequestException as exc:
        raise S4HCError(f"{what}: token endpoint unreachable ({exc.__class__.__name__})", "SAP_UNAVAILABLE") from exc
    try:
        body = r.json()
    except ValueError:
        body = {}
    if r.status_code != 200 or not isinstance(body, dict) or not body.get("access_token"):
        reason = (body.get("error_description") or body.get("error")) if isinstance(body, dict) else None
        raise S4HCError(f"{what}: token request refused (HTTP {r.status_code}"
                        + (f": {str(reason)[:200]}" if reason else "") + ")", "SAP_UNAUTHORIZED")
    try:
        expires_in = float(body.get("expires_in") or 300)
    except (TypeError, ValueError):
        expires_in = 300.0
    return str(body["access_token"]), time.time() + max(expires_in, 1.0)


# --------------------------------------------------------------- S/4HANA OAuth
class ClientCredentialsToken:
    """Client-credentials token from S/4HANA's OAuth server, refreshed before expiry.

    On S/4HANA Cloud the OAuth client is a communication user whose inbound
    authentication method is "OAuth 2.0" (Communication Systems app); a
    password-only user answers ``invalid_client: OAuth 2.0 client is unknown``."""

    def __init__(self, token_url: str, client_id: str, client_secret: str, scope: str | None = None) -> None:
        self.token_url, self.client_id, self._secret, self.scope = token_url, client_id, client_secret, scope
        self._token: tuple[str, float] | None = None
        self._lock = threading.Lock()

    def headers(self) -> dict[str, str]:
        with self._lock:
            if self._token is None or self._token[1] - TOKEN_SKEW_SECONDS < time.time():
                data = {"grant_type": "client_credentials"}
                if self.scope:
                    data["scope"] = self.scope
                self._token = _token_request(self.token_url, data, self.client_id, self._secret,
                                             "S/4HANA OAuth client credentials")
            return {"Authorization": f"Bearer {self._token[0]}"}


# --------------------------------------------------------------- BTP bindings
def _vcap_credentials(label: str) -> dict[str, Any] | None:
    raw = os.getenv("VCAP_SERVICES")
    if not raw:
        return None
    try:
        services = json.loads(raw)
    except ValueError:
        raise S4HCError("VCAP_SERVICES is not valid JSON", "NOT_CONFIGURED") from None
    for instance in services.get(label, []) if isinstance(services, dict) else []:
        creds = instance.get("credentials") if isinstance(instance, dict) else None
        if isinstance(creds, dict):
            return creds
    return None


@dataclass(frozen=True)
class ServiceBinding:
    """Credentials of a BTP service instance (from VCAP_SERVICES or env)."""
    client_id: str
    client_secret: str = field(repr=False)
    token_url: str = ""
    service_url: str = ""
    proxy_url: str = ""


def _https(url: str, what: str) -> str:
    parts = urlsplit(url or "")
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise S4HCError(f"{what} must be an https URL without credentials", "NOT_CONFIGURED")
    return url.rstrip("/")


def destination_binding() -> ServiceBinding:
    creds = _vcap_credentials("destination")
    if creds:
        return ServiceBinding(str(creds.get("clientid", "")), str(creds.get("clientsecret", "")),
                              _https(str(creds.get("url", "")).rstrip("/") + "/oauth/token", "destination UAA url"),
                              service_url=_https(str(creds.get("uri", "")), "destination service uri"))
    cid, secret = os.getenv("S4HC_DESTINATION_CLIENT_ID"), os.getenv("S4HC_DESTINATION_CLIENT_SECRET")
    if not (cid and secret and os.getenv("S4HC_DESTINATION_SERVICE_URL") and os.getenv("S4HC_DESTINATION_TOKEN_URL")):
        raise S4HCError("BTP Destination service not bound: bind a destination instance (VCAP_SERVICES) or set "
                        "S4HC_DESTINATION_SERVICE_URL, S4HC_DESTINATION_TOKEN_URL, S4HC_DESTINATION_CLIENT_ID "
                        "and S4HC_DESTINATION_CLIENT_SECRET from a service key", "NOT_CONFIGURED")
    return ServiceBinding(cid, secret, _https(os.environ["S4HC_DESTINATION_TOKEN_URL"], "S4HC_DESTINATION_TOKEN_URL"),
                          service_url=_https(os.environ["S4HC_DESTINATION_SERVICE_URL"], "S4HC_DESTINATION_SERVICE_URL"))


def connectivity_binding() -> ServiceBinding:
    creds = _vcap_credentials("connectivity")
    if creds:
        host = creds.get("onpremise_proxy_host")
        port = creds.get("onpremise_proxy_http_port") or creds.get("onpremise_proxy_port")
        token_url = creds.get("token_service_url") or (str(creds.get("url", "")).rstrip("/") + "/oauth/token")
        return ServiceBinding(str(creds.get("clientid", "")), str(creds.get("clientsecret", "")),
                              _https(str(token_url), "connectivity token url"), proxy_url=f"http://{host}:{port}")
    cid, secret, proxy = (os.getenv("S4HC_CONNECTIVITY_CLIENT_ID"), os.getenv("S4HC_CONNECTIVITY_CLIENT_SECRET"),
                          os.getenv("S4HC_CONNECTIVITY_PROXY"))
    if not (cid and secret and proxy and os.getenv("S4HC_CONNECTIVITY_TOKEN_URL")):
        raise S4HCError("OnPremise destination needs the BTP Connectivity service: bind a connectivity instance "
                        "(only reachable from BTP Cloud Foundry / Kyma) or set S4HC_CONNECTIVITY_PROXY, "
                        "S4HC_CONNECTIVITY_TOKEN_URL, S4HC_CONNECTIVITY_CLIENT_ID and "
                        "S4HC_CONNECTIVITY_CLIENT_SECRET", "NOT_CONFIGURED")
    if urlsplit(proxy).scheme != "http":
        raise S4HCError("S4HC_CONNECTIVITY_PROXY must be http://host:port (the in-cluster connectivity proxy)",
                        "NOT_CONFIGURED")
    return ServiceBinding(cid, secret, _https(os.environ["S4HC_CONNECTIVITY_TOKEN_URL"], "S4HC_CONNECTIVITY_TOKEN_URL"),
                          proxy_url=proxy)


# ------------------------------------------------------------ resolved target
@dataclass(frozen=True)
class SapTarget:
    """Everything the OData client needs to reach S/4HANA for one identity."""
    base_url: str
    headers: dict[str, str] = field(repr=False)
    expires_at: float
    proxy_url: str | None = None
    basic: tuple[str, str] | None = field(default=None, repr=False)
    sap_client: str | None = None
    authentication: str = ""


# Destination authentication types: those answered with a token, and those that carry the end user.
BEARER_TYPES = {"OAuth2SAMLBearerAssertion", "OAuth2UserTokenExchange", "OAuth2JWTBearer",
                "OAuth2ClientCredentials", "OAuth2Password", "OAuth2TechnicalUserPropagation", "SAMLAssertion"}
USER_TYPES = {"OAuth2SAMLBearerAssertion", "OAuth2UserTokenExchange", "OAuth2JWTBearer", "PrincipalPropagation",
              "SAMLAssertion"}


def _destination_url(url: str, proxy_type: str) -> str:
    """Destination URLs are operator configuration, not caller input, so no SAP-domain
    allowlist applies (private cloud hosts are customer DNS names). OnPremise URLs are
    Cloud Connector virtual hosts addressed through the connectivity tunnel and may be
    http; Internet destinations must be https."""
    parts = urlsplit(url or "")
    on_prem = proxy_type == "OnPremise"
    if (parts.scheme not in (("http", "https") if on_prem else ("https",)) or not parts.hostname
            or parts.username or parts.password or parts.query or parts.fragment or parts.path not in ("", "/")):
        raise S4HCError(f"Destination URL must be a plain {'http(s)' if on_prem else 'https'} origin "
                        "(the OData service paths are appended by this server)", "NOT_CONFIGURED")
    return url.rstrip("/")


class DestinationService:
    """Reads one destination, with or without a user token, and turns the answer
    into a SapTarget. Fails closed: an authTokens entry with an error, a user
    destination without a user token, or a user destination that yields no
    per-user credential is refused; nothing ever falls back to a shared identity."""

    def __init__(self, binding: ServiceBinding | None = None, connectivity: ServiceBinding | None = None) -> None:
        self._binding = binding
        self._connectivity = connectivity
        self._service_token: tuple[str, float] | None = None
        self._cache: OrderedDict[str, SapTarget] = OrderedDict()
        self._lock = threading.Lock()

    @property
    def binding(self) -> ServiceBinding:
        if self._binding is None:
            self._binding = destination_binding()
        return self._binding

    def _token(self) -> str:
        with self._lock:
            if self._service_token is None or self._service_token[1] - TOKEN_SKEW_SECONDS < time.time():
                b = self.binding
                self._service_token = _token_request(b.token_url, {"grant_type": "client_credentials"},
                                                     b.client_id, b.client_secret, "BTP Destination service")
            return self._service_token[0]

    def resolve(self, name: str, user_jwt: str | None = None) -> SapTarget:
        """Per-destination, per-user-token cache (the key is an HMAC of the token, so
        one user's credential can never be served to another)."""
        cache_key = _key(name, user_jwt or "")
        with self._lock:
            hit = self._cache.get(cache_key)
            if hit and hit.expires_at - TOKEN_SKEW_SECONDS > time.time():
                self._cache.move_to_end(cache_key)
                return hit
        target = self._lookup(name, user_jwt)
        with self._lock:
            self._cache[cache_key] = target
            while len(self._cache) > MAX_CACHED_USER_CREDENTIALS:
                self._cache.popitem(last=False)
        return target

    def _lookup(self, name: str, user_jwt: str | None) -> SapTarget:
        url = f"{self.binding.service_url}/destination-configuration/v1/destinations/{quote(name, safe='')}"
        headers = {"Authorization": f"Bearer {self._token()}", "Accept": "application/json"}
        if user_jwt:
            headers["X-User-Token"] = user_jwt
        try:
            r = requests.get(url, headers=headers, timeout=HTTP_TIMEOUT, allow_redirects=False)
        except requests.RequestException as exc:
            raise S4HCError(f"BTP Destination service unreachable ({exc.__class__.__name__})", "SAP_UNAVAILABLE") from exc
        if r.status_code == 404:
            raise S4HCError(f"BTP destination {name!r} not found", "NOT_CONFIGURED")
        if r.status_code != 200:
            raise S4HCError(f"BTP Destination service returned HTTP {r.status_code} for {name!r}", "SAP_UNAVAILABLE")
        try:
            data = r.json()
        except ValueError:
            raise S4HCError("BTP Destination service returned non-JSON", "SAP_UNAVAILABLE") from None
        conf = data.get("destinationConfiguration") if isinstance(data, dict) else None
        if not isinstance(conf, dict):
            raise S4HCError("BTP Destination service response has no destinationConfiguration", "SAP_UNAVAILABLE")
        tokens = data.get("authTokens") or []
        return self._target(name, conf, tokens if isinstance(tokens, list) else [], user_jwt)

    def _target(self, name: str, conf: dict, auth_tokens: list, user_jwt: str | None) -> SapTarget:
        auth = str(conf.get("Authentication") or "NoAuthentication")
        proxy_type = str(conf.get("ProxyType") or "Internet")
        base_url = _destination_url(str(conf.get("URL") or ""), proxy_type)
        if auth in USER_TYPES and not user_jwt:
            raise S4HCError(f"Destination {name!r} uses {auth}, which propagates the end user: it needs an "
                            "authenticated user token (S4HC_SAP_AUTH=principal_propagation with OIDC sign-in)",
                            "UNAUTHORIZED_SCOPE")
        headers: dict[str, str] = {}
        expires_at = time.time() + 300
        for t in auth_tokens:
            if not isinstance(t, dict):
                continue
            if t.get("error"):
                raise S4HCError(f"Destination {name!r} could not issue a credential: {str(t['error'])[:200]}",
                                "SAP_UNAUTHORIZED")
            hh = t.get("http_header") if isinstance(t.get("http_header"), dict) else None
            if hh and hh.get("key") and hh.get("value"):
                headers[str(hh["key"])] = str(hh["value"])
            elif str(t.get("type", "")).lower() == "bearer" and t.get("value"):
                headers["Authorization"] = f"Bearer {t['value']}"
            try:
                expires_at = min(expires_at, time.time() + float(t.get("expires_in") or 300))
            except (TypeError, ValueError):
                pass
        if auth == "SAMLAssertion" and "Authorization" in headers:
            headers["x-sap-security-session"] = "create"  # same as ARC-1 / SAP Business Application Studio
        basic = None
        if auth == "BasicAuthentication":
            if not conf.get("User") or not conf.get("Password"):
                raise S4HCError(f"Destination {name!r} is BasicAuthentication without User/Password", "NOT_CONFIGURED")
            basic = (str(conf["User"]), str(conf["Password"]))
        elif auth in BEARER_TYPES and "Authorization" not in headers:
            raise S4HCError(f"Destination {name!r} ({auth}) returned no token; check the destination, the "
                            "trust between BTP and S/4HANA, and the user's mapping", "SAP_UNAUTHORIZED")
        elif auth not in BEARER_TYPES | {"PrincipalPropagation", "BasicAuthentication", "NoAuthentication"}:
            raise S4HCError(f"Destination authentication {auth!r} is not supported by this server", "NOT_CONFIGURED")

        proxy_url = None
        if proxy_type == "OnPremise":
            conn = self._connectivity or connectivity_binding()
            self._connectivity = conn
            if auth == "PrincipalPropagation":
                # SAP's recommended variant: exchange the user token at the connectivity
                # service (jwt-bearer) and send it as Proxy-Authorization; Cloud Connector
                # derives the per-user X.509 certificate from it.
                token, exp = _token_request(conn.token_url, {
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": str(user_jwt),
                    "token_format": "jwt", "response_type": "token"}, conn.client_id, conn.client_secret,
                    "BTP Connectivity user token exchange")
                headers.pop("SAP-Connectivity-Authentication", None)
            else:
                token, exp = _token_request(conn.token_url, {"grant_type": "client_credentials"},
                                            conn.client_id, conn.client_secret, "BTP Connectivity service")
            expires_at = min(expires_at, exp)
            headers["Proxy-Authorization"] = f"Bearer {token}"
            if conf.get("CloudConnectorLocationId"):
                headers["SAP-Connectivity-SCC-Location_ID"] = str(conf["CloudConnectorLocationId"])
            proxy_url = conn.proxy_url
        elif auth == "PrincipalPropagation":
            raise S4HCError(f"Destination {name!r}: PrincipalPropagation needs ProxyType OnPremise (Cloud Connector)",
                            "NOT_CONFIGURED")
        return SapTarget(base_url=base_url, headers=headers, expires_at=expires_at, proxy_url=proxy_url, basic=basic,
                         sap_client=str(conf["sap-client"]) if conf.get("sap-client") else None, authentication=auth)


_destination_service: DestinationService | None = None
_client_credentials: dict[str, ClientCredentialsToken] = {}
_lock = threading.Lock()


def destination_service() -> DestinationService:
    global _destination_service
    with _lock:
        if _destination_service is None:
            _destination_service = DestinationService()
        return _destination_service


def client_credentials_token(base_url: str) -> ClientCredentialsToken:
    cid, secret = os.getenv("S4HC_OAUTH_CLIENT_ID"), os.getenv("S4HC_OAUTH_CLIENT_SECRET")
    if not (cid and secret):
        raise S4HCError("S4HC_SAP_AUTH=oauth2_client_credentials needs S4HC_OAUTH_CLIENT_ID and "
                        "S4HC_OAUTH_CLIENT_SECRET (an S/4HANA communication user with OAuth 2.0)", "NOT_CONFIGURED")
    token_url = _https(os.getenv("S4HC_OAUTH_TOKEN_URL") or f"{base_url}/sap/bc/sec/oauth2/token", "S4HC_OAUTH_TOKEN_URL")
    k = _key(token_url, cid, secret)
    with _lock:
        if k not in _client_credentials:
            _client_credentials[k] = ClientCredentialsToken(token_url, cid, secret, os.getenv("S4HC_OAUTH_SCOPE") or None)
        return _client_credentials[k]


def reset() -> None:
    """Drop cached bindings and tokens (tests, credential rotation)."""
    global _destination_service
    with _lock:
        _destination_service = None
        _client_credentials.clear()
