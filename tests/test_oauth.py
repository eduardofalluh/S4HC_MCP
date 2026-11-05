"""OAuth 2.0 inbound (who calls the MCP) and outbound (who the MCP is at SAP),
including BTP principal propagation for public and private cloud. The BTP
Destination / Connectivity services and the SAP token endpoint are faked here;
every token, key and secret is generated per run."""

from __future__ import annotations

import asyncio
import base64
import json
import secrets
import time

import pytest
from fastmcp.server.auth import AccessToken
from fastmcp.server.auth.providers.jwt import RSAKeyPair

import fake_s4hc
from src.s4hc import dependencies, inbound_auth, sap_auth
from src.s4hc.client import S4HCError

DEST_SVC = "https://destination.example.cfapps.eu10.hana.ondemand.com"
DEST_UAA = "https://sub.authentication.eu10.hana.ondemand.com/oauth/token"
CONN_UAA = "https://sub.authentication.eu10.hana.ondemand.com/conn/oauth/token"
ISSUER = "https://sub.authentication.eu10.hana.ondemand.com/oauth/token"
AUDIENCE = "s4hc-mcp!t1"
RUN = {k: secrets.token_urlsafe(12) for k in ("dest_secret", "conn_secret", "cc_secret", "basic_pw", "api_key")}


class Resp:
    def __init__(self, status, payload=None):
        self.status_code, self._payload, self.text = status, payload, json.dumps(payload) if payload else ""

    def json(self):
        if self._payload is None:
            raise ValueError
        return self._payload

    def close(self):
        pass


def _sub(jwt: str) -> str:
    payload = jwt.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))["sub"]


class FakeBTP:
    """Token endpoints (Destination UAA, Connectivity UAA, S/4HANA OAuth) and the
    Destination service, recording what was asked."""

    def __init__(self):
        self.posts: list[dict] = []
        self.lookups: list[dict] = []
        self.destinations = {
            "S4_PUBLIC_PP": {"URL": "https://my-api.s4hana.cloud.sap", "ProxyType": "Internet",
                             "Authentication": "OAuth2SAMLBearerAssertion"},
            "S4_PRIVATE_PP": {"URL": "http://s4d-pp:50101", "ProxyType": "OnPremise",
                              "Authentication": "PrincipalPropagation", "sap-client": "100",
                              "CloudConnectorLocationId": "LOC1"},
            "S4_TECH_BASIC": {"URL": "https://my-api.s4hana.cloud.sap", "ProxyType": "Internet",
                              "Authentication": "BasicAuthentication", "User": "COMM_TECH", "Password": RUN["basic_pw"]},
            "S4_BROKEN": {"URL": "https://my-api.s4hana.cloud.sap", "ProxyType": "Internet",
                          "Authentication": "OAuth2SAMLBearerAssertion"},
            "S4_EVIL_URL": {"URL": "https://my-api.s4hana.cloud.sap/sap?x=1", "ProxyType": "Internet",
                            "Authentication": "BasicAuthentication", "User": "u", "Password": RUN["basic_pw"]},
            "S4_TECH_CC": {"URL": "https://my-api.s4hana.cloud.sap", "ProxyType": "Internet",
                           "Authentication": "OAuth2ClientCredentials"},
            "S4_SAML_SYSTEMUSER": {"URL": "https://my-api.s4hana.cloud.sap", "ProxyType": "Internet",
                                   "Authentication": "OAuth2SAMLBearerAssertion", "SystemUser": "COMM_TECH"},
            "S4_ONPREM_TECH": {"URL": "http://s4d-tech:50101", "ProxyType": "OnPremise",
                               "Authentication": "BasicAuthentication", "User": "COMM_TECH", "Password": RUN["basic_pw"]},
            "S4_ONPREM_HTTPS": {"URL": "https://s4d-pp:44301", "ProxyType": "OnPremise",
                                "Authentication": "PrincipalPropagation"},
        }

    def post(self, url, data=None, auth=None, **kw):
        self.posts.append({"url": url, "data": dict(data or {}), "auth": auth})
        if url == DEST_UAA and auth == ("dest-client", RUN["dest_secret"]):
            return Resp(200, {"access_token": "dest-svc-token", "expires_in": 3600})
        if url == CONN_UAA and auth == ("conn-client", RUN["conn_secret"]):
            if data.get("grant_type") == "urn:ietf:params:oauth:grant-type:jwt-bearer":
                return Resp(200, {"access_token": f"conn-user-{_sub(data['assertion'])}", "expires_in": 600})
            return Resp(200, {"access_token": "conn-tech", "expires_in": 600})
        if url.endswith("/sap/bc/sec/oauth2/token"):
            if auth == ("S4_OAUTH_CLIENT", RUN["cc_secret"]):
                return Resp(200, {"access_token": "s4-cc-token", "expires_in": 3600, "token_type": "Bearer"})
            return Resp(401, {"error": "invalid_client", "error_description": "OAuth 2.0 client is unknown"})
        return Resp(401, {"error": "invalid_client"})

    def get(self, url, headers=None, **kw):
        headers = headers or {}
        if not url.startswith(f"{DEST_SVC}/destination-configuration/v1/destinations/"):
            return Resp(404, None)
        name = url.rsplit("/", 1)[1]
        user = headers.get("X-User-Token")
        self.lookups.append({"name": name, "user": user})
        if bearer_sent(url, headers, **kw) != "Bearer dest-svc-token":
            return Resp(401, {"error": "unauthorized"})
        if name not in self.destinations:
            return Resp(404, {"ErrorMessage": "not found"})
        conf = dict(self.destinations[name], Name=name)
        tokens = []
        if name == "S4_BROKEN" and user:
            tokens = [{"type": "", "value": "", "error": "Retrieval of OAuthToken failed: SAML trust missing"}]
        elif conf["Authentication"] == "OAuth2ClientCredentials":
            tokens = [{"type": "Bearer", "value": "s4-tech-token", "expires_in": "3600"}]
        elif conf["Authentication"] == "OAuth2SAMLBearerAssertion" and conf.get("SystemUser"):
            tokens = [{"type": "Bearer", "value": "s4-system-user-token", "expires_in": "3600"}]
        elif conf["Authentication"] == "OAuth2SAMLBearerAssertion" and user:
            tok = f"s4-user-{_sub(user)}"
            tokens = [{"type": "Bearer", "value": tok, "expires_in": "3600",
                       "http_header": {"key": "Authorization", "value": f"Bearer {tok}"}}]
        elif conf["Authentication"] == "PrincipalPropagation" and user:
            tokens = [{"type": "", "value": "",
                       "http_header": {"key": "SAP-Connectivity-Authentication", "value": f"Bearer {user}"}}]
        return Resp(200, {"owner": {"SubaccountId": "sa"}, "destinationConfiguration": conf, "authTokens": tokens})


KEYS = RSAKeyPair.generate()


def user_jwt(sub: str, **kw) -> str:
    return KEYS.create_token(subject=sub, issuer=ISSUER, audience=AUDIENCE, **kw)


def signed_in(sub: str) -> AccessToken:
    return AccessToken(token=user_jwt(sub), client_id="mcp-client", scopes=[], subject=sub,
                       claims={"sub": sub, "iss": ISSUER, "aud": AUDIENCE, "exp": int(time.time()) + 3600})


@pytest.fixture
def btp(monkeypatch):
    fake = FakeBTP()
    monkeypatch.setattr(sap_auth.requests, "post", fake.post)
    monkeypatch.setattr(sap_auth.requests, "get", fake.get)
    for k, v in {"S4HC_DESTINATION_SERVICE_URL": DEST_SVC, "S4HC_DESTINATION_TOKEN_URL": DEST_UAA,
                 "S4HC_DESTINATION_CLIENT_ID": "dest-client", "S4HC_DESTINATION_CLIENT_SECRET": RUN["dest_secret"],
                 "S4HC_CONNECTIVITY_PROXY": "http://connectivityproxy.internal:20003",
                 "S4HC_CONNECTIVITY_TOKEN_URL": CONN_UAA, "S4HC_CONNECTIVITY_CLIENT_ID": "conn-client",
                 "S4HC_CONNECTIVITY_CLIENT_SECRET": RUN["conn_secret"]}.items():
        monkeypatch.setenv(k, v)
    return fake


@pytest.fixture
def as_user(monkeypatch):
    """Make the next tool calls look like they come from a signed-in user."""
    current: dict = {"token": None}
    monkeypatch.setattr(dependencies, "_access_token", lambda ctx: current["token"])

    def _set(token):
        current["token"] = token
    return _set


def odata_calls(calls):
    return [c for c in calls if "/sap/opu/odata/" in c["path"]]


def basic_header(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def bearer_sent(url, headers=None, **kw):
    """The Authorization header requests would really send for this call (auth objects and ~/.netrc included)."""
    import requests as _rq
    return _rq.Session().prepare_request(_rq.Request("GET", url, headers=headers, auth=kw.get("auth"))).headers.get(
        "Authorization")


def sap_checks(accepted: set[str], seen: list):
    """A fake S/4HANA $metadata endpoint for the user-token verifier."""
    def fake_get(url, headers=None, **kw):
        sent = bearer_sent(url, headers, **kw)
        seen.append(sent)
        return Resp(200 if sent in {f"Bearer {t}" for t in accepted} else 401, None)
    return fake_get


def pp(monkeypatch, destination):
    monkeypatch.setenv("S4HC_SAP_AUTH", "principal_propagation")
    monkeypatch.setenv("S4HC_BTP_DESTINATION", destination)


# ------------------------------------------------- OAuth client credentials
def test_client_credentials_replace_the_password(tool, calls, btp, monkeypatch):
    monkeypatch.setenv("S4HC_SAP_AUTH", "oauth2_client_credentials")
    monkeypatch.setenv("S4HC_OAUTH_CLIENT_ID", "S4_OAUTH_CLIENT")
    monkeypatch.setenv("S4HC_OAUTH_CLIENT_SECRET", RUN["cc_secret"])
    fake_s4hc.STATE["valid_bearers"] = {"s4-cc-token"}
    assert tool("list_company_codes")(ctx=None)["count"] == 2
    tool("get_cost_center_actuals")(ctx=None, fiscal_year=2026)
    assert len(btp.posts) == 1  # one token, reused
    assert btp.posts[0]["url"] == "https://my-api.s4hana.cloud.sap/sap/bc/sec/oauth2/token"
    assert btp.posts[0]["data"] == {"grant_type": "client_credentials"}
    assert all(c["auth"] is None and c["headers"]["Authorization"] == "Bearer s4-cc-token" for c in odata_calls(calls))
    assert tool("s4hc_health")(ctx=None)["auth_mode"] == "oauth2-client-credentials"


def test_client_credentials_refusal_is_clear_and_secret_free(tool, btp, monkeypatch):
    monkeypatch.setenv("S4HC_SAP_AUTH", "oauth2_client_credentials")
    monkeypatch.setenv("S4HC_OAUTH_CLIENT_ID", "Z_PASSWORD_ONLY_USER")
    monkeypatch.setenv("S4HC_OAUTH_CLIENT_SECRET", RUN["cc_secret"])
    with pytest.raises(S4HCError, match="OAuth 2.0 client is unknown") as exc:
        tool("list_company_codes")(ctx=None)
    assert exc.value.code == "SAP_UNAUTHORIZED" and RUN["cc_secret"] not in str(exc.value)


def test_client_credentials_token_is_refreshed_before_expiry(btp):
    cc = sap_auth.ClientCredentialsToken("https://my-api.s4hana.cloud.sap/sap/bc/sec/oauth2/token",
                                         "S4_OAUTH_CLIENT", RUN["cc_secret"])
    cc.headers()
    cc._token = (cc._token[0], time.time() + 30)  # inside the 60 s refresh window
    cc.headers()
    assert len(btp.posts) == 2


# ------------------------------------------------------- technical destination
def test_technical_destination_supplies_host_and_credentials(tool, calls, btp, monkeypatch):
    monkeypatch.setenv("S4HC_SAP_AUTH", "destination")
    monkeypatch.setenv("S4HC_BTP_DESTINATION", "S4_TECH_BASIC")
    monkeypatch.delenv("S4HC_BASE_URL")
    assert tool("list_company_codes")(ctx=None)["count"] == 2
    assert odata_calls(calls)[0]["headers"]["Authorization"] == basic_header("COMM_TECH", RUN["basic_pw"])
    assert odata_calls(calls)[0]["auth"] is None  # per request, so a rotated password is picked up
    assert odata_calls(calls)[0]["host"] == "my-api.s4hana.cloud.sap" and btp.lookups[0]["user"] is None
    info = tool("s4hc_health")(ctx=None)
    assert info["auth_mode"] == "btp-destination" and RUN["basic_pw"] not in json.dumps(info)


def test_user_destination_is_refused_without_a_user(tool, btp, monkeypatch):
    monkeypatch.setenv("S4HC_SAP_AUTH", "destination")
    monkeypatch.setenv("S4HC_BTP_DESTINATION", "S4_PUBLIC_PP")
    with pytest.raises(S4HCError, match="propagates the end user"):
        tool("list_company_codes")(ctx=None)


def test_destination_url_must_be_a_plain_origin(tool, btp, monkeypatch):
    monkeypatch.setenv("S4HC_SAP_AUTH", "destination")
    monkeypatch.setenv("S4HC_BTP_DESTINATION", "S4_EVIL_URL")
    with pytest.raises(S4HCError, match="plain https origin"):
        tool("list_company_codes")(ctx=None)


def test_destination_credential_rotation_and_url_change_are_picked_up(tool, calls, btp, monkeypatch):
    monkeypatch.setenv("S4HC_SAP_AUTH", "destination")
    monkeypatch.setenv("S4HC_BTP_DESTINATION", "S4_TECH_BASIC")
    tool("get_cost_center_actuals")(ctx=None, fiscal_year=2026)
    rotated = secrets.token_urlsafe(12)
    btp.destinations["S4_TECH_BASIC"]["Password"] = rotated
    sap_auth.destination_service()._cache.clear()  # what the lookup cache's expiry does
    calls.clear()
    tool("get_cost_center_actuals")(ctx=None, fiscal_year=2026)
    assert {c["headers"]["Authorization"] for c in odata_calls(calls)} == {basic_header("COMM_TECH", rotated)}
    btp.destinations["S4_TECH_BASIC"]["URL"] = "https://other-api.s4hana.cloud.sap"
    sap_auth.destination_service()._cache.clear()
    calls.clear()
    tool("get_cost_center_actuals")(ctx=None, fiscal_year=2026)
    assert {c["host"] for c in odata_calls(calls)} == {"other-api.s4hana.cloud.sap"}


def test_master_data_is_not_served_from_the_old_system_after_a_repoint(tool, calls, btp, monkeypatch):
    monkeypatch.setenv("S4HC_SAP_AUTH", "destination")
    monkeypatch.setenv("S4HC_BTP_DESTINATION", "S4_TECH_BASIC")
    tool("list_company_codes")(ctx=None)
    calls.clear()
    tool("list_company_codes")(ctx=None)
    assert not odata_calls(calls)  # same system: served from the master-data cache
    for change in ({"URL": "https://other-api.s4hana.cloud.sap"}, {"sap-client": "200"}):
        btp.destinations["S4_TECH_BASIC"].update(change)
        sap_auth.destination_service()._cache.clear()
        calls.clear()
        tool("list_company_codes")(ctx=None)
        assert {c["host"] for c in odata_calls(calls)} == {"other-api.s4hana.cloud.sap"}  # fetched again


def test_destination_change_during_a_call_fails_instead_of_resending(monkeypatch):
    targets = iter([sap_auth.SapTarget("https://my-api.s4hana.cloud.sap", {"Authorization": "Bearer a"}, 0),
                    sap_auth.SapTarget("https://other-api.s4hana.cloud.sap", {"Authorization": "Bearer b"}, 0)])
    headers = dependencies._target_headers(lambda: next(targets), "https://my-api.s4hana.cloud.sap", None)
    assert headers() == {"Authorization": "Bearer a"}
    with pytest.raises(S4HCError, match="changed during this call"):
        headers()


def test_on_premise_destination_must_be_http(tool, btp, as_user, monkeypatch):
    pp(monkeypatch, "S4_ONPREM_HTTPS")
    as_user(signed_in("alice"))
    with pytest.raises(S4HCError, match="plain http origin.*Cloud Connector"):
        tool("list_company_codes")(ctx=None)
    assert not [p for p in btp.posts if p["url"] == CONN_UAA]  # no user exchange for a URL that cannot work


@pytest.mark.parametrize("token_service_url,expected", [
    ("https://sub.authentication.eu10.hana.ondemand.com", "https://sub.authentication.eu10.hana.ondemand.com/oauth/token"),
    ("https://sub.authentication.eu10.hana.ondemand.com/", "https://sub.authentication.eu10.hana.ondemand.com/oauth/token"),
    ("https://sub.authentication.eu10.hana.ondemand.com/oauth/token",
     "https://sub.authentication.eu10.hana.ondemand.com/oauth/token"),
    ("https://tenant.accounts.ondemand.com/oauth2/token", "https://tenant.accounts.ondemand.com/oauth2/token"),
])
def test_connectivity_binding_from_vcap_services(monkeypatch, token_service_url, expected):
    monkeypatch.setenv("VCAP_SERVICES", json.dumps({"connectivity": [{"credentials": {
        "clientid": "conn-client", "clientsecret": RUN["conn_secret"], "token_service_url": token_service_url,
        "onpremise_proxy_host": "connectivityproxy.internal", "onpremise_proxy_http_port": "20003"}}]}))
    b = sap_auth.connectivity_binding()
    assert (b.token_url, b.proxy_url) == (expected, "http://connectivityproxy.internal:20003")
    assert RUN["conn_secret"] not in repr(b)


def test_connectivity_binding_without_proxy_is_refused(monkeypatch):
    monkeypatch.setenv("VCAP_SERVICES", json.dumps({"connectivity": [{"credentials": {
        "clientid": "conn-client", "clientsecret": RUN["conn_secret"],
        "token_service_url": "https://sub.authentication.eu10.hana.ondemand.com"}}]}))
    with pytest.raises(S4HCError, match="onpremise_proxy_host"):
        sap_auth.connectivity_binding()


def test_destination_binding_from_vcap_services(monkeypatch):
    for k in ("S4HC_DESTINATION_SERVICE_URL", "S4HC_DESTINATION_TOKEN_URL", "S4HC_DESTINATION_CLIENT_ID",
              "S4HC_DESTINATION_CLIENT_SECRET"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("VCAP_SERVICES", json.dumps({"destination": [{"credentials": {
        "clientid": "dest-client", "clientsecret": RUN["dest_secret"], "uri": DEST_SVC,
        "url": "https://sub.authentication.eu10.hana.ondemand.com"}}]}))
    b = sap_auth.destination_binding()
    assert (b.service_url, b.token_url, b.client_id) == (DEST_SVC, DEST_UAA, "dest-client")
    assert RUN["dest_secret"] not in repr(b)


# ---------------------------------------- principal propagation, public cloud
def test_public_cloud_pp_runs_each_call_as_its_user(tool, calls, btp, as_user, monkeypatch):
    pp(monkeypatch, "S4_PUBLIC_PP")
    fake_s4hc.STATE["valid_bearers"] = {"s4-user-alice", "s4-user-bob"}
    alice, bob = signed_in("alice"), signed_in("bob")
    as_user(alice)
    tool("list_company_codes")(ctx=None)
    as_user(bob)
    tool("get_cost_center_actuals")(ctx=None, fiscal_year=2026)
    as_user(alice)
    tool("cost_center_summary")(ctx=None, fiscal_year=2026)
    used = [c["headers"]["Authorization"] for c in odata_calls(calls)]
    assert set(used) == {"Bearer s4-user-alice", "Bearer s4-user-bob"}
    assert all(c["auth"] is None for c in odata_calls(calls))  # never a shared password
    assert [lk["user"] for lk in btp.lookups] == [alice.token, bob.token]  # alice's second call hit the cache


def test_pp_master_data_cache_is_per_user(tool, calls, btp, as_user, monkeypatch):
    pp(monkeypatch, "S4_PUBLIC_PP")
    as_user(signed_in("alice"))
    tool("list_company_codes")(ctx=None)
    n = len(odata_calls(calls))
    as_user(signed_in("bob"))
    tool("list_company_codes")(ctx=None)
    assert len(odata_calls(calls)) == 2 * n  # bob is not served alice's cached result


@pytest.mark.parametrize("token", [None, AccessToken(token="api-key", client_id="api-key", scopes=[],
                                                     claims={"s4hc_api_key": True})])
def test_pp_never_falls_back_to_a_shared_identity(tool, calls, btp, as_user, monkeypatch, token):
    pp(monkeypatch, "S4_PUBLIC_PP")
    as_user(token)
    with pytest.raises(S4HCError, match="UNAUTHORIZED_SCOPE"):
        tool("list_company_codes")(ctx=None)
    assert not odata_calls(calls) and not btp.lookups


@pytest.mark.parametrize("destination", ["S4_TECH_BASIC", "S4_TECH_CC", "S4_SAML_SYSTEMUSER", "S4_ONPREM_TECH"])
def test_pp_refuses_a_technical_destination(tool, calls, btp, as_user, monkeypatch, destination):
    """A technical credential would run every signed-in user as one SAP identity."""
    pp(monkeypatch, destination)
    as_user(signed_in("alice"))
    with pytest.raises(S4HCError, match="shared technical identity") as exc:
        tool("list_company_codes")(ctx=None)
    assert exc.value.code == "UNAUTHORIZED_SCOPE"
    assert not odata_calls(calls) and not [p for p in btp.posts if p["url"] == CONN_UAA]


API_KEY_CALLER = AccessToken(token="api-key", client_id="api-key", scopes=[], claims={"s4hc_api_key": True})


def test_mixed_mode_signed_in_users_as_themselves_api_key_as_before(tool, calls, btp, as_user, monkeypatch):
    """S4HC_MIXED_AUTH (ARC-1: SAP_PP_STRICT=false): both ways of connecting on one deployment."""
    pp(monkeypatch, "S4_PUBLIC_PP")
    monkeypatch.setenv("S4HC_MIXED_AUTH", "true")
    as_user(signed_in("alice"))
    tool("list_company_codes")(ctx=None)
    assert {c["headers"].get("Authorization") for c in odata_calls(calls)} == {"Bearer s4-user-alice"}
    assert all(c["auth"] is None for c in odata_calls(calls))
    for caller in (API_KEY_CALLER, None):  # API key over HTTP, or stdio
        calls.clear()
        as_user(caller)
        tool("get_cost_center_actuals")(ctx=None, fiscal_year=2026)
        from conftest import TEST_PASSWORD, TEST_USER
        assert {c["auth"] for c in odata_calls(calls)} == {(TEST_USER, TEST_PASSWORD)}  # the communication user
    assert len(btp.lookups) == 1 and _sub(btp.lookups[0]["user"]) == "alice"  # only alice's call went through BTP


def test_mixed_mode_api_key_caller_can_bring_tenant_headers(btp, as_user, monkeypatch):
    from test_review_fixes import Ctx
    pp(monkeypatch, "S4_PUBLIC_PP")
    monkeypatch.setenv("S4HC_MIXED_AUTH", "true")
    as_user(API_KEY_CALLER)
    conn = dependencies.resolve_connection(Ctx({"x-s4hc-base-url": "https://tenant-api.s4hana.cloud.sap",
                                                "x-s4hc-user": "comm", "x-s4hc-password": RUN["basic_pw"]}))
    assert (conn.source, conn.base_url) == ("header", "https://tenant-api.s4hana.cloud.sap")


def test_mixed_mode_never_moves_a_signed_in_user_to_the_shared_account(tool, calls, btp, as_user, monkeypatch):
    from test_review_fixes import Ctx
    pp(monkeypatch, "S4_BROKEN")
    monkeypatch.setenv("S4HC_MIXED_AUTH", "true")
    as_user(signed_in("alice"))
    with pytest.raises(S4HCError, match="SAML trust missing"):
        tool("list_company_codes")(ctx=None)
    assert not odata_calls(calls)  # refused, not retried as the communication user
    with pytest.raises(S4HCError, match="not accepted"):
        dependencies.resolve_connection(Ctx({"x-s4hc-user": "comm", "x-s4hc-password": RUN["basic_pw"]}))


def test_mixed_mode_with_the_s4hana_sign_in(tool, calls, as_user, monkeypatch):
    monkeypatch.setenv("S4HC_SAP_AUTH", "user_token")
    monkeypatch.setenv("S4HC_MIXED_AUTH", "true")
    fake_s4hc.STATE["valid_bearers"] = {"s4-opaque-alice"}
    as_user(AccessToken(token="s4-opaque-alice", client_id="s4hana-oauth", scopes=[],
                        claims={"s4hc_sap_user_token": True}))
    tool("list_company_codes")(ctx=None)
    assert {c["headers"]["Authorization"] for c in odata_calls(calls)} == {"Bearer s4-opaque-alice"}
    fake_s4hc.STATE["valid_bearers"] = None
    calls.clear()
    as_user(API_KEY_CALLER)
    tool("get_cost_center_actuals")(ctx=None, fiscal_year=2026)
    assert all(c["auth"] is not None for c in odata_calls(calls))


@pytest.mark.parametrize("reserved", ["s4hc_api_key", "s4hc_sap_user_token"])
def test_a_sign_in_token_cannot_claim_to_be_the_api_key(reserved):
    """In mixed mode the API key reaches the communication user, so its marker must not be forgeable."""
    tok = user_jwt("alice", additional_claims={reserved: True})
    assert asyncio.run(strict_verifier().verify_token(tok)) is None


def test_mixed_mode_reports_a_missing_destination(tool, monkeypatch):
    monkeypatch.setenv("S4HC_SAP_AUTH", "principal_propagation")
    monkeypatch.setenv("S4HC_MIXED_AUTH", "true")
    info = tool("s4hc_health")(ctx=None)
    assert info["auth_mode"] == "basic" and info["signed_in_users"] == "none"  # the API-key path still works
    assert "S4HC_BTP_DESTINATION" in info["signed_in_users_error"]


def test_mixed_mode_shows_both_paths_in_health(tool, btp, as_user, monkeypatch):
    pp(monkeypatch, "S4_PUBLIC_PP")
    info = tool("s4hc_health")(ctx=None)
    assert (info["auth_mode"], info["signed_in_users"], info["mixed_auth"]) == (
        "principal-propagation", "principal-propagation", False)
    monkeypatch.setenv("S4HC_MIXED_AUTH", "true")
    info = tool("s4hc_health")(ctx=None)
    assert (info["status"], info["auth_mode"], info["signed_in_users"], info["mixed_auth"]) == (
        "configured", "basic", "principal-propagation", True)


def test_pp_refuses_header_credentials(btp, as_user, monkeypatch):
    from test_review_fixes import Ctx
    pp(monkeypatch, "S4_PUBLIC_PP")
    as_user(signed_in("alice"))
    with pytest.raises(S4HCError, match="not accepted"):
        dependencies.get_client(Ctx({"x-s4hc-user": "comm", "x-s4hc-password": RUN["basic_pw"]}))


def test_pp_destination_error_fails_closed(tool, calls, btp, as_user, monkeypatch):
    pp(monkeypatch, "S4_BROKEN")
    as_user(signed_in("alice"))
    with pytest.raises(S4HCError, match="SAML trust missing"):
        tool("list_company_codes")(ctx=None)
    assert not odata_calls(calls)


# --------------------------------------- principal propagation, private cloud
def test_private_cloud_pp_goes_through_cloud_connector(tool, calls, btp, as_user, monkeypatch):
    pp(monkeypatch, "S4_PRIVATE_PP")
    alice = signed_in("alice")
    as_user(alice)
    tool("list_company_codes")(ctx=None)
    c = odata_calls(calls)[0]
    assert (c["scheme"], c["host"]) == ("http", "s4d-pp:50101")  # Cloud Connector virtual host
    assert c["proxies"] == {"http": "http://connectivityproxy.internal:20003",
                            "https": "http://connectivityproxy.internal:20003"}
    assert c["headers"]["Proxy-Authorization"] == "Bearer conn-user-alice"  # user exchange, not the tech token
    assert c["headers"]["SAP-Connectivity-SCC-Location_ID"] == "LOC1" and c["headers"]["sap-client"] == "100"
    assert "SAP-Connectivity-Authentication" not in c["headers"] and c["auth"] is None
    exchange = [p for p in btp.posts if p["url"] == CONN_UAA][0]
    assert exchange["data"]["grant_type"].endswith("jwt-bearer") and exchange["data"]["assertion"] == alice.token


def test_on_premise_without_connectivity_binding_is_refused(tool, btp, as_user, monkeypatch):
    pp(monkeypatch, "S4_PRIVATE_PP")
    monkeypatch.delenv("S4HC_CONNECTIVITY_PROXY")
    as_user(signed_in("alice"))
    with pytest.raises(S4HCError, match="Connectivity service"):
        tool("list_company_codes")(ctx=None)


# --------------------------------------------------------- SAP user token
def test_user_token_mode_forwards_the_signed_in_token(tool, calls, as_user, monkeypatch):
    monkeypatch.setenv("S4HC_SAP_AUTH", "user_token")
    fake_s4hc.STATE["valid_bearers"] = {"s4-opaque-alice"}
    as_user(AccessToken(token="s4-opaque-alice", client_id="s4hana-oauth", scopes=[],
                        claims={"s4hc_sap_user_token": True}))
    assert tool("list_company_codes")(ctx=None)["count"] == 2
    assert {c["headers"]["Authorization"] for c in odata_calls(calls)} == {"Bearer s4-opaque-alice"}
    as_user(None)
    with pytest.raises(S4HCError, match="UNAUTHORIZED_SCOPE"):
        tool("list_company_codes")(ctx=None)


def test_sap_user_token_verifier_asks_sap_and_caches(monkeypatch):
    seen = []
    monkeypatch.setattr(inbound_auth.requests, "get", sap_checks({"good"}, seen))
    v = inbound_auth.SapUserTokenVerifier()
    assert asyncio.run(v.verify_token("good")).claims == {"s4hc_sap_user_token": True}
    assert asyncio.run(v.verify_token("good")) is not None
    assert asyncio.run(v.verify_token("bad")) is None
    assert seen == ["Bearer good", "Bearer bad"]  # the second check of "good" came from the cache
    assert asyncio.run(v.verify_token("bad")) is None
    assert seen == ["Bearer good", "Bearer bad"]  # and so did the second refusal of "bad"


def test_sap_user_token_checks_are_bounded(monkeypatch):
    """Unauthenticated callers cannot use this server to drive unbounded traffic at SAP."""
    seen = []
    monkeypatch.setattr(inbound_auth.requests, "get", sap_checks({"good"}, seen))
    monkeypatch.setenv("S4HC_SAP_TOKEN_CHECKS_PER_MIN", "3")
    v = inbound_auth.SapUserTokenVerifier()
    assert asyncio.run(v.verify_token("good")) is not None
    for i in range(10):
        assert asyncio.run(v.verify_token(f"guess-{i}")) is None
    assert len(seen) == 3  # one accepted token + two guesses; the rest refused without asking SAP
    assert asyncio.run(v.verify_token("good")) is not None  # an accepted token keeps working from the cache


def test_a_flood_of_invented_tokens_never_logs_out_a_signed_in_user(monkeypatch):
    accepted, seen = {"good"}, []
    monkeypatch.setattr(inbound_auth.requests, "get", sap_checks(accepted, seen))
    monkeypatch.setenv("S4HC_SAP_TOKEN_CHECKS_PER_MIN", "3")
    v = inbound_auth.SapUserTokenVerifier()
    assert asyncio.run(v.verify_token("good")) is not None
    for i in range(10):
        asyncio.run(v.verify_token(f"guess-{i}"))  # budget used up by strangers
    age(v, inbound_auth.SAP_TOKEN_CHECK_TTL + 1)  # the accepted entry's 5 minutes are over
    n = len(seen)
    assert asyncio.run(v.verify_token("good")) is not None and len(seen) == n + 1  # re-checked with SAP anyway
    accepted.discard("good")  # SAP revokes it
    age(v, inbound_auth.SAP_TOKEN_CHECK_TTL + 1)
    assert asyncio.run(v.verify_token("good")) is None
    assert asyncio.run(v.verify_token("good")) is None and len(seen) == n + 2  # the revocation is cached


def age(v, seconds):
    """Pretend SAP last confirmed every accepted token `seconds` ago."""
    for k in v._ok:
        v._ok[k] = (time.time() - seconds, 0.0)


def test_one_recheck_per_token_however_many_requests_arrive(monkeypatch):
    seen = []
    check = sap_checks({"good"}, seen)

    def slow_get(url, headers=None, **kw):
        time.sleep(0.2)  # SAP answering slowly: the other requests arrive meanwhile
        return check(url, headers, **kw)

    monkeypatch.setattr(inbound_auth.requests, "get", slow_get)
    v = inbound_auth.SapUserTokenVerifier()
    assert asyncio.run(v.verify_token("good")) is not None
    age(v, inbound_auth.SAP_TOKEN_CHECK_TTL + 1)

    async def burst():
        return await asyncio.gather(*(v.verify_token("good") for _ in range(50)))
    assert all(t is not None for t in asyncio.run(burst()))  # served from the just-expired entry
    assert len(seen) == 2  # the first check plus one re-check, not 50


def test_recheck_backs_off_when_sap_does_not_answer(monkeypatch):
    answers = []

    def fake_get(url, headers=None, **kw):
        answers.append(1)
        return Resp(200 if len(answers) == 1 else 503, None)

    monkeypatch.setattr(inbound_auth.requests, "get", fake_get)
    v = inbound_auth.SapUserTokenVerifier()
    assert asyncio.run(v.verify_token("good")) is not None
    age(v, inbound_auth.SAP_TOKEN_CHECK_TTL + 1)
    assert asyncio.run(v.verify_token("good")) is not None  # SAP down: still within the unconfirmed limit
    assert asyncio.run(v.verify_token("good")) is not None and len(answers) == 2  # backing off, not re-asking
    age(v, inbound_auth.SAP_TOKEN_MAX_UNCONFIRMED + 1)
    assert asyncio.run(v.verify_token("good")) is None  # unconfirmed for too long


def test_one_sender_cannot_use_up_the_token_check_budget_for_others(monkeypatch):
    seen, sender = [], {"addr": "203.0.113.9"}
    monkeypatch.setattr(inbound_auth.requests, "get", sap_checks({"valid-new"}, seen))
    monkeypatch.setattr(inbound_auth, "_request_source", lambda hops: sender["addr"])
    monkeypatch.setenv("S4HC_SAP_TOKEN_CHECKS_PER_SOURCE_PER_MIN", "2")
    v = inbound_auth.SapUserTokenVerifier()
    for i in range(20):
        asyncio.run(v.verify_token(f"guess-{i}"))
    assert len(seen) == 2  # the flooding sender's share
    sender["addr"] = "198.51.100.7"  # GenAI Studio's egress, say
    assert asyncio.run(v.verify_token("valid-new")) is not None


@pytest.mark.parametrize("peer,xff,hops,expected", [
    ("10.0.0.5", "", 0, "10.0.0.5"),
    ("10.0.0.5", "6.6.6.6, 203.0.113.9", 0, "10.0.0.5"),  # no trusted proxy: the header is ignored
    ("10.0.0.5", "6.6.6.6, 203.0.113.9", 1, "203.0.113.9"),  # appended by the trusted ingress; 6.6.6.6 is forged
    ("10.0.0.5", "6.6.6.6, 203.0.113.9, 10.0.0.9", 2, "203.0.113.9"),
    ("10.0.0.5", "203.0.113.9", 2, "10.0.0.5"),  # shorter chain than configured: did not come through the proxies
    ("10.0.0.5", "6.6.6.6, 203.0.113.9:4711", 1, "203.0.113.9"),  # a per-connection port is not a new sender
    ("10.0.0.5", "6.6.6.6, [2001:db8::7]:443", 1, "2001:db8::7"),
    ("10.0.0.5", "6.6.6.6, 2001:db8::7", 1, "2001:db8::7"),
])
def test_token_check_sender_is_taken_from_trusted_proxies_only(peer, xff, hops, expected):
    assert inbound_auth._source_of(peer, xff, hops) == expected


def test_token_check_sender_reads_every_forwarded_for_line(monkeypatch):
    """A proxy that adds its own X-Forwarded-For line must not let the caller's forged line win."""
    from fastmcp.server.dependencies import _current_http_request
    from starlette.requests import Request
    request = Request({"type": "http", "method": "POST", "path": "/mcp", "client": ("10.0.0.5", 50000),
                       "headers": [(b"x-forwarded-for", b"6.6.6.6"), (b"x-forwarded-for", b"203.0.113.9")]})
    token = _current_http_request.set(request)
    try:
        assert inbound_auth._request_source(1) == "203.0.113.9"
        assert inbound_auth._request_source(0) == "10.0.0.5"
    finally:
        _current_http_request.reset(token)


def test_each_sap_user_token_is_its_own_session_principal(monkeypatch):
    from mcp.server.auth.provider import principal_components
    monkeypatch.setattr(inbound_auth.requests, "get", sap_checks({"token-a", "token-b"}, []))
    v = inbound_auth.SapUserTokenVerifier()
    a, b = asyncio.run(v.verify_token("token-a")), asyncio.run(v.verify_token("token-b"))
    assert principal_components(a) != principal_components(b)
    assert principal_components(a) == principal_components(asyncio.run(v.verify_token("token-a")))
    assert "token-a" not in a.subject


def test_netrc_never_replaces_header_credentials(tmp_path, monkeypatch, as_user, btp):
    """requests swaps in ~/.netrc credentials when a request carries no auth object."""
    import requests as _rq
    netrc = tmp_path / "netrc"
    netrc.write_text(f"default login shared password {RUN['basic_pw']}\n")
    netrc.chmod(0o600)
    monkeypatch.setenv("NETRC", str(netrc))
    assert bearer_sent("https://my-api.s4hana.cloud.sap/x").startswith("Basic ")  # the trap is real
    monkeypatch.setenv("S4HC_SAP_AUTH", "user_token")
    as_user(AccessToken(token="s4-opaque-alice", client_id="s4hana-oauth", scopes=[],
                        claims={"s4hc_sap_user_token": True}))
    client = dependencies.get_client(None)
    prepared = client.session.prepare_request(_rq.Request("GET", f"{client.base_url}/sap/opu/odata/sap/X"))
    assert prepared.headers["Authorization"] == "Bearer s4-opaque-alice"
    assert sap_auth.destination_service().resolve("S4_TECH_BASIC").base_url  # the lookup's Bearer survives too
    monkeypatch.setattr(inbound_auth.requests, "get", sap_checks({"good"}, []))
    assert asyncio.run(inbound_auth.SapUserTokenVerifier().verify_token("bad")) is None


def test_sap_user_token_unreachable_sap_is_not_cached_as_refused(monkeypatch):
    import requests as _requests
    answers = iter([_requests.ConnectionError(), Resp(200, None)])

    def fake_get(url, headers=None, **kw):
        a = next(answers)
        if isinstance(a, Exception):
            raise a
        return a

    monkeypatch.setattr(inbound_auth.requests, "get", fake_get)
    v = inbound_auth.SapUserTokenVerifier()
    assert asyncio.run(v.verify_token("good")) is None
    assert asyncio.run(v.verify_token("good")) is not None  # retried once SAP answers


# ---------------------------------------------------------- inbound OIDC
def strict_verifier():
    return inbound_auth.StrictJWTVerifier(public_key=KEYS.public_key, issuer=ISSUER, audience=AUDIENCE)


def test_oidc_accepts_a_valid_token():
    tok = asyncio.run(strict_verifier().verify_token(user_jwt("alice")))
    assert tok is not None and tok.claims["sub"] == "alice"


@pytest.mark.parametrize("bad", [
    lambda: user_jwt("alice", expires_in_seconds=-120),
    lambda: KEYS.create_token(subject="alice", issuer="https://evil.example/oauth/token", audience=AUDIENCE),
    lambda: KEYS.create_token(subject="alice", issuer=ISSUER, audience="other-app!t1"),
    lambda: RSAKeyPair.generate().create_token(subject="alice", issuer=ISSUER, audience=AUDIENCE),
    lambda: user_jwt("alice", additional_claims={"nbf": int(time.time()) + 3600}),
])
def test_oidc_refuses_bad_tokens(bad):
    assert asyncio.run(strict_verifier().verify_token(bad())) is None


def test_oidc_refuses_a_token_without_expiry():
    import jwt as pyjwt
    tok = pyjwt.encode({"sub": "alice", "iss": ISSUER, "aud": AUDIENCE}, KEYS.private_key.get_secret_value(),
                       algorithm="RS256")
    assert asyncio.run(strict_verifier().verify_token(tok)) is None


def test_api_key_is_accepted_but_carries_no_user():
    v = inbound_auth.ApiKeyOrVerifier(strict_verifier(), RUN["api_key"])
    key = asyncio.run(v.verify_token(RUN["api_key"]))
    assert key.claims == {"s4hc_api_key": True} and key.token == "api-key"
    assert asyncio.run(v.verify_token(RUN["api_key"] + "x")) is None
    assert asyncio.run(v.verify_token(user_jwt("bob"))).claims["sub"] == "bob"


@pytest.mark.parametrize("env,match", [
    ({"S4HC_OIDC_ISSUER": ISSUER}, "S4HC_OIDC_AUDIENCE is required"),
    ({"S4HC_OIDC_ISSUER": ISSUER, "S4HC_OIDC_AUDIENCE": AUDIENCE, "S4HC_PUBLIC_URL": "https://mcp.example"},
     "exactly one"),
    ({"S4HC_OIDC_ISSUER": ISSUER, "S4HC_OIDC_AUDIENCE": AUDIENCE, "S4HC_OIDC_JWKS_URI": "http://idp/keys",
      "S4HC_PUBLIC_URL": "https://mcp.example"}, "must be https"),
    ({"S4HC_SAP_AUTH": "principal_propagation"}, "needs signed-in users"),
    ({"S4HC_SAP_AUTH": "user_token", "S4HC_OIDC_ISSUER": ISSUER}, "cannot be combined"),
])
def test_auth_misconfiguration_fails_at_startup(monkeypatch, env, match):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(S4HCError, match=match):
        inbound_auth.build_auth_provider()


def test_signed_in_users_get_their_own_rate_limit_bucket_on_a_shared_identity(as_user):
    as_user(signed_in("alice"))
    alice, trusted = dependencies.rate_limit_key(None)
    assert trusted and alice == dependencies.rate_limit_key(None)[0]
    as_user(signed_in("bob"))
    bob = dependencies.rate_limit_key(None)[0]
    as_user(None)
    shared = dependencies.rate_limit_key(None)[0]
    assert len({alice, bob, shared}) == 3
    assert shared == dependencies.resolve_connection(None).fingerprint


@pytest.mark.parametrize("env,status,auth_mode", [
    ({"S4HC_SAP_AUTH": "oauth2_client_credentials", "S4HC_OAUTH_CLIENT_ID": "S4_OAUTH_CLIENT"}, "unconfigured", "none"),
    ({"S4HC_SAP_AUTH": "destination"}, "unconfigured", "none"),
    ({"S4HC_SAP_AUTH": "principal_propagation"}, "unconfigured", "none"),
    ({"S4HC_SAP_AUTH": "principal_propagation", "S4HC_BTP_DESTINATION": "S4_PUBLIC_PP"}, "configured",
     "principal-propagation"),
    ({"S4HC_SAP_AUTH": "x509"}, "unconfigured", "none"),
])
def test_health_reports_missing_settings(tool, monkeypatch, env, status, auth_mode):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    info = tool("s4hc_health")(ctx=None)
    assert (info["status"], info["auth_mode"]) == (status, auth_mode)


def test_explicit_basic_mode_ignores_a_configured_certificate(monkeypatch):
    monkeypatch.setenv("S4HC_CLIENT_CERT", "/nonexistent/cert.pem")
    monkeypatch.setenv("S4HC_CLIENT_KEY", "/nonexistent/key.pem")
    assert dependencies.resolve_connection(None).cert == ("/nonexistent/cert.pem", "/nonexistent/key.pem")
    monkeypatch.setenv("S4HC_SAP_AUTH", "basic")
    conn = dependencies.resolve_connection(None)
    assert conn.cert is None and conn.password


def test_audit_uses_the_verified_subject(as_user):
    as_user(signed_in("alice"))
    assert dependencies.caller_identity(None) == "sub:alice"
    as_user(AccessToken(token="api-key", client_id="api-key", scopes=[], claims={"s4hc_api_key": True}))
    assert dependencies.caller_identity(None) == "api-key"


# ------------------------------------------------- real server over HTTP
def _serve(tmp_path_factory, extra_env):
    import os
    import socket
    import subprocess
    import sys
    import textwrap

    from test_http_e2e import ROOT, TESTS, _free_port
    shim = tmp_path_factory.mktemp("oauth-shim")
    (shim / "sitecustomize.py").write_text(textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {TESTS!r})
        import requests, fake_s4hc
        requests.Session.get = fake_s4hc.fake_get
    """))
    port = _free_port()
    env = {k: v for k, v in os.environ.items() if not k.startswith("S4HC_")}
    env.update({"PYTHONPATH": str(shim), "LOG_LEVEL": "WARNING", "S4HC_BASE_URL": "https://my-api.s4hana.cloud.sap",
                "S4HC_USER": "comm_user", "S4HC_PASSWORD": RUN["basic_pw"],
                "S4HC_PUBLIC_URL": f"http://127.0.0.1:{port}", **extra_env})
    proc = subprocess.Popen([sys.executable, "server.py", "--http", "--port", str(port)], cwd=ROOT, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            break
        except OSError:
            if proc.poll() is not None:
                pytest.fail("server exited: " + proc.stderr.read())
            time.sleep(0.2)
    return proc, f"http://127.0.0.1:{port}"


@pytest.fixture(scope="module")
def oidc_server(tmp_path_factory):
    proc, url = _serve(tmp_path_factory, {"S4HC_OIDC_ISSUER": ISSUER, "S4HC_OIDC_AUDIENCE": AUDIENCE,
                                          "S4HC_OIDC_PUBLIC_KEY": KEYS.public_key, "S4HC_MCP_API_KEY": RUN["api_key"]})
    yield url
    proc.kill()


def test_http_oidc_challenge_metadata_and_tokens(oidc_server):
    import requests

    from test_http_e2e import call
    mcp = f"{oidc_server}/mcp"
    r = requests.post(mcp, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                      headers={"Accept": "application/json, text/event-stream"})
    assert r.status_code == 401 and "resource_metadata=" in r.headers.get("WWW-Authenticate", "")
    prm = requests.get(f"{oidc_server}/.well-known/oauth-protected-resource/mcp").json()
    assert prm["authorization_servers"] == [ISSUER] and prm["resource"] == mcp
    out = call(mcp, "list_company_codes", headers={"Authorization": f"Bearer {user_jwt('alice')}"})
    assert out["count"] == 2
    wrong_aud = KEYS.create_token(subject="alice", issuer=ISSUER, audience="other-app!t1")
    r = requests.post(mcp, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                      headers={"Accept": "application/json, text/event-stream", "Authorization": f"Bearer {wrong_aud}"})
    assert r.status_code == 401
    assert call(mcp, "s4hc_health", headers={"Authorization": f"Bearer {RUN['api_key']}"})["mcp_auth"] == "oidc"
    # Clients set up for the API key alone send it as x-api-key; that keeps working with OIDC on.
    assert call(mcp, "list_company_codes", headers={"x-api-key": RUN["api_key"]})["count"] == 2
    r = requests.post(mcp, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                      headers={"Accept": "application/json, text/event-stream", "x-api-key": RUN["api_key"] + "x"})
    assert r.status_code == 401


@pytest.fixture(scope="module")
def mixed_server(tmp_path_factory):
    proc, url = _serve(tmp_path_factory, {"S4HC_OIDC_ISSUER": ISSUER, "S4HC_OIDC_AUDIENCE": AUDIENCE,
                                          "S4HC_OIDC_PUBLIC_KEY": KEYS.public_key, "S4HC_MCP_API_KEY": RUN["api_key"],
                                          "S4HC_SAP_AUTH": "principal_propagation", "S4HC_BTP_DESTINATION": "S4",
                                          "S4HC_MIXED_AUTH": "true"})
    yield url
    proc.kill()


def test_http_mixed_mode_both_ways_of_connecting(mixed_server):
    from test_http_e2e import call
    mcp = f"{mixed_server}/mcp"
    out = call(mcp, "list_company_codes", headers={"x-api-key": RUN["api_key"]})
    assert out["count"] == 2  # the previous setup: API key + communication user
    health = call(mcp, "s4hc_health", headers={"Authorization": f"Bearer {RUN['api_key']}"})
    assert (health["auth_mode"], health["signed_in_users"], health["mixed_auth"]) == (
        "basic", "principal-propagation", True)
    # A signed-in user goes through BTP (not bound here) and is refused, not served as the communication user.
    out = call(mcp, "list_company_codes", headers={"Authorization": f"Bearer {user_jwt('alice')}"})
    assert "BTP Destination service not bound" in out["__error__"]


@pytest.fixture(scope="module")
def sap_oauth_server(tmp_path_factory):
    proc, url = _serve(tmp_path_factory, {"S4HC_SAP_AUTH": "user_token",
                                          "S4HC_SAP_OAUTH_SCOPES": "API_JOURNALENTRYITEMBASIC_SRV_0001"})
    yield url
    proc.kill()


def test_http_sap_oauth_advertises_s4hana_sign_in(sap_oauth_server):
    import requests
    prm = requests.get(f"{sap_oauth_server}/.well-known/oauth-protected-resource/mcp").json()
    assert [a.rstrip("/") for a in prm["authorization_servers"]] == [sap_oauth_server]  # this server describes S/4HANA's AS
    assert meta_issuer_matches(sap_oauth_server)
    meta = requests.get(f"{sap_oauth_server}/.well-known/oauth-authorization-server").json()
    assert meta["authorization_endpoint"] == "https://my.s4hana.cloud.sap/sap/bc/sec/oauth2/authorize"
    # S/4HANA Cloud signs business users in with public PKCE clients: the /sap/public/ token endpoint,
    # no client secret (as on a live tenant with the MCP Inspector, 2026-11-05).
    assert meta["token_endpoint"] == "https://my-api.s4hana.cloud.sap/sap/public/bc/sec/oauth2/token"
    assert "none" in meta["token_endpoint_auth_methods_supported"]
    assert meta["code_challenge_methods_supported"] == ["S256"]
    assert meta["scopes_supported"] == ["API_JOURNALENTRYITEMBASIC_SRV_0001"]
    r = requests.post(f"{sap_oauth_server}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                      headers={"Accept": "application/json, text/event-stream"})
    assert r.status_code == 401


def meta_issuer_matches(base):
    import requests
    return requests.get(f"{base}/.well-known/oauth-authorization-server").json()["issuer"] == base
