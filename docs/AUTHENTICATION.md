# Authentication and principal propagation

Two separate hops, each with its own choice (same split ARC-1 uses):

```
AI client ──(1) who is calling?──▶ S4HC_MCP ──(2) who are we at SAP?──▶ S/4HANA Cloud (public or private)
```

| Hop | Setting | Modes |
|---|---|---|
| 1. AI client → MCP | `S4HC_MCP_API_KEY`, `S4HC_OIDC_*` | none (stdio/loopback), API key, **OIDC/JWT** (XSUAA, SAP Cloud Identity Services, Entra ID…), **S/4HANA sign-in** |
| 2. MCP → S/4HANA | `S4HC_SAP_AUTH` | `basic` (default), `x509`, **`oauth2_client_credentials`**, **`destination`**, **`principal_propagation`**, **`user_token`** |

Bold = added in this change. Defaults are unchanged: without the new variables the server behaves exactly as before.
Unset, `S4HC_SAP_AUTH` keeps the old rule (client certificate if one is configured, else the password);
`basic` forces the password and `x509` requires a certificate.

## Which combination to pick

| You want | Hop 1 | Hop 2 | Needs |
|---|---|---|---|
| Today's setup, one technical identity | API key | `basic` or `x509` | Communication user |
| Same, but no password on the wire | API key or OIDC | `oauth2_client_credentials` | Communication user with inbound auth "OAuth 2.0" |
| Know *who* called (audit, per-user rate limit), shared SAP identity | OIDC | any technical mode | An IdP that issues JWTs for this server |
| **Each user runs in S/4HANA as themselves — public cloud** | OIDC (XSUAA of the BTP subaccount) | `principal_propagation` + `OAuth2SAMLBearerAssertion` destination | BTP subaccount, Destination service, SAML trust BTP → S/4HANA |
| **Each user as themselves — private cloud / on-premise** | OIDC (XSUAA) | `principal_propagation` + `PrincipalPropagation` destination | Above + Connectivity service + Cloud Connector, **server runs on BTP CF/Kyma** |
| Each user as themselves, **no BTP** (CALM_MCP pattern) — **verified live** | S/4HANA sign-in | `user_token` | Communication system with an OAuth 2.0 public client (authorization code + PKCE), arrangement on a scenario that allows OAuth 2.0; MCP client that does OAuth (GenAI Studio does for CALM) |

**Per-user identity needs a communication scenario that allows OAuth 2.0.** S/4HANA decides this per
scenario, and no setting on this server changes it: `SAP_COM_0087` (SAP Analytics Cloud for Planning
Integration) allows only Basic and certificate authentication, so neither per-user route (S/4HANA sign-in
or BTP SAML bearer) works on an arrangement of it. `SAP_COM_0241` (Finance - Cloud Consolidation Data
Collection Integration) allows OAuth 2.0 and contains `API_JOURNALENTRYITEMBASIC_SRV`, `API_COMPANYCODE_SRV`,
`API_GLACCOUNTINCHARTOFACCOUNTS_SRV` and `API_GLACCOUNTLINEITEM`, but not the cost center or profit center
master services or the plan service (`API_FINPLANNINGENTRYITEM_SRV`): for signed-in users the cost center
and profit center tools then fall back to the journal entry service, without names, and `get_actual_vs_plan`
answers `PLAN_UNAVAILABLE`, unless another OAuth-capable arrangement covers those services.
The arrangement page shows which methods a scenario allows: picking OAuth 2.0 on one that does not answer
"This scenario doesn't support this authentication method".

Per-user modes are strict by default, like ARC-1's `SAP_PP_STRICT=true`: a call without a signed-in user
(stdio, API key) is refused, and so is a `principal_propagation` destination whose Authentication is a technical one
(`BasicAuthentication`, `OAuth2ClientCredentials`, `NoAuthentication`, or a SAML bearer destination with a
`SystemUser`); nothing ever falls back to a shared SAP identity. The data-scope allowlists still apply on
top, as a second layer.

**Both ways of connecting on one deployment** (`S4HC_MIXED_AUTH=true`, ARC-1's supported mixed mode,
`SAP_PP_STRICT=false`): users who sign in run in SAP as themselves, and callers with the API key (and stdio)
use the previous setup, the communication user (`S4HC_USER` / `S4HC_PASSWORD` or a certificate) or the
`x-s4hc-*` tenant headers. Which path a call takes depends only on how the caller authenticated: a signed-in
user is never moved to the shared account (if their per-user call fails, it is refused), and sending
`x-s4hc-*` headers with a user sign-in is still refused. `s4hc_health` shows both (`auth_mode` for this
caller, `signed_in_users` for signed-in users) and the startup log names both.

In mixed mode **the API key grants the communication user's SAP rights**, which may be broader than any
single user's: treat it like that account's password. A connection that sends the key never gets the
`401` that starts the sign-in, so everyone on it silently uses the shared account. Give the key only to the
connections meant to use the shared account (automation, a "shared" connection in GenAI Studio), and set up
personal sign-in as a separate connection that does not send it. A sign-in token that carries a claim named
`s4hc_*` (reserved for this server's own markers) is refused, so an identity provider's claims cannot make a
user look like the API key.

With OIDC, each signed-in user gets their own rate-limit bucket (`S4HC_RATE_LIMIT_PER_MIN`), also on a
shared SAP identity; `S4HC_RATE_LIMIT_GLOBAL_PER_MIN` still caps them together.

## What ARC-1 does, and what we took from it

[ARC-1](https://github.com/arc-mcp/arc-1) (TypeScript MCP server for ABAP development over ADT, v1.10.0,
read 2026-11-04) is the most complete open implementation of per-user SAP identity for MCP. Its design:

- **Hop 1:** XSUAA OAuth proxy (it serves `/authorize`, `/token`, `/register` for MCP clients), or an OIDC
  resource server validating JWTs against the IdP's JWKS and publishing RFC 9728 metadata, or API keys
  mapped to scope profiles ([`src/server/http.ts`](https://github.com/arc-mcp/arc-1/blob/main/src/server/http.ts)).
- **Hop 2, per user:** it never builds SAP credentials itself. It passes the user's JWT to the BTP
  Destination service (`X-User-Token`, via SAP Cloud SDK `getDestination({jwt, isolationStrategy: 'tenant-user'})`
  in [`@arc-mcp/xsuaa-auth`](https://github.com/arc-mcp/xsuaa-auth/blob/main/src/btp/destination.ts)) and uses
  whatever per-user credential comes back, in this order
  ([`applyPerUserAuthTokens`](https://github.com/arc-mcp/arc-1/blob/main/src/server/server.ts)):
  1. `Proxy-Authorization` with a jwt-bearer-exchanged user token → Cloud Connector (on-premise, SAP's recommended variant)
  2. `SAP-Connectivity-Authentication: Bearer <user JWT>` → Cloud Connector (older variant)
  3. `Authorization: Bearer` from `OAuth2UserTokenExchange` / `OAuth2SAMLBearerAssertion` (cloud to cloud)
  4. `Authorization: SAML2.0 <assertion>` + `x-sap-security-session: create` from a `SAMLAssertion`
     destination (S/4HANA Public Cloud **ADT**, the same destination SAP Business Application Studio uses)
- Any `authTokens[].error` aborts the call; per-user caches never mix users; the SAP password field is
  cleared whenever a per-user credential is used.

**Public cloud** (ARC-1 [`s4hana-public-cloud.md`](https://github.com/arc-mcp/arc-1/blob/main/docs_page/s4hana-public-cloud.md)):
no Cloud Connector. BTP's subaccount signing certificate is trusted by S/4HANA through a Communication
System with **SAML Bearer Assertion Provider = ON**; the destination mints a SAML assertion per user
(NameID = e-mail) and S/4HANA maps it to the business user with that e-mail.

**Private cloud / on-premise** ([`principal-propagation-setup.md`](https://github.com/arc-mcp/arc-1/blob/main/docs_page/principal-propagation-setup.md)):
destination `ProxyType=OnPremise`, `Authentication=PrincipalPropagation`; the request goes through the BTP
Connectivity proxy to Cloud Connector, which issues a short-lived X.509 certificate (subject e.g.
`CN=${email}`); SAP trusts Cloud Connector's CA in STRUST and maps the certificate to a user with CERTRULE
(issuer-restricted, never a `CN=*` catch-all); `icm/trusted_reverse_proxy_<n>` must match Cloud Connector's
system certificate exactly.

**What differs for us.** ARC-1 talks to ADT, which SAP's API Policy FAQ (Q33) classifies as internal
development endpoints — ARC-1's own docs say that channel needs SAP clarification. S4HC_MCP only reads
**published OData business APIs** through communication arrangements, which is the documented purpose of
those APIs. For OData APIs on S/4HANA Cloud the per-user destination type is therefore
**`OAuth2SAMLBearerAssertion`** (a Bearer token from S/4HANA's own OAuth server, scoped to the
communication arrangement), not `SAMLAssertion` (ADT/UI). This server supports all four ARC-1 credential
shapes; the table above names the one to configure.

Also taken from ARC-1: the `exp`/`nbf`/audience rules for JWTs, refusing a shared fallback, keeping the
API key as an identity without a user, and logging the verified subject instead of a caller-supplied header.

## Setup per mode

### Hop 1 — OIDC / JWT (`S4HC_OIDC_*`)

| Variable | Example |
|---|---|
| `S4HC_OIDC_ISSUER` | XSUAA: `https://<subdomain>.authentication.<region>.hana.ondemand.com/oauth/token` · IAS: `https://<tenant>.accounts.ondemand.com` · Entra: `https://login.microsoftonline.com/<tenant>/v2.0` |
| `S4HC_OIDC_JWKS_URI` | XSUAA: `https://<subdomain>.authentication.<region>.hana.ondemand.com/token_keys` · IAS: `.../oauth2/certs` · Entra: `.../discovery/v2.0/keys` |
| `S4HC_OIDC_AUDIENCE` | **Required.** XSUAA: the xsappname/client id (`s4hc-mcp!t12345`); Entra: `api://<app-id>` |
| `S4HC_PUBLIC_URL` | This server's public URL, used in the RFC 9728 document |
| optional | `S4HC_OIDC_REQUIRED_SCOPES`, `S4HC_OIDC_SCOPES` (advertised), `S4HC_OIDC_AUTHORIZATION_SERVERS`, `S4HC_OIDC_ALGORITHM` (RS256), `S4HC_OIDC_PUBLIC_KEY` (static key instead of JWKS) |

Tokens need a valid signature, issuer, audience, `exp`, `sub` and (if present) `nbf`. Unauthenticated calls
get `401` with `WWW-Authenticate: Bearer resource_metadata=".../.well-known/oauth-protected-resource/mcp"`,
which is how MCP clients find the sign-in. `S4HC_MCP_API_KEY` stays valid, as `Authorization: Bearer <key>`
or `x-api-key: <key>`; it carries no user.

### Hop 2 — `oauth2_client_credentials`

S/4HANA Cloud: in **Communication Systems**, give the inbound user authentication method **OAuth 2.0**
(this registers the OAuth client) and use it in the communication arrangement. Then set
`S4HC_OAUTH_CLIENT_ID`, `S4HC_OAUTH_CLIENT_SECRET`, optional `S4HC_OAUTH_SCOPE` and `S4HC_OAUTH_TOKEN_URL`
(default `https://<tenant>-api.s4hana.cloud.sap/sap/bc/sec/oauth2/token`). Tokens are cached and refreshed
60 s before expiry.

Checked on the Syntax Gamma tenant (2026-11-04): the token endpoint answers, and a password-only
communication user is refused with `invalid_client: OAuth 2.0 client is unknown` — the tenant change above
is needed first. **Whether S/4HANA Cloud grants `client_credentials` to such a client is not yet verified;
check it on the tenant before relying on this mode.**

### Hop 2 — `destination` and `principal_propagation` (BTP Destination service)

Set `S4HC_BTP_DESTINATION=<name>` and bind the Destination service: on Cloud Foundry bind an instance
(`VCAP_SERVICES` is read); elsewhere set `S4HC_DESTINATION_SERVICE_URL` (`uri`),
`S4HC_DESTINATION_TOKEN_URL` (`url`; `/oauth/token` is added when it has no path), `S4HC_DESTINATION_CLIENT_ID`,
`S4HC_DESTINATION_CLIENT_SECRET` from a service key. The destination supplies the SAP host; `S4HC_BASE_URL`
is not used in these modes.

**Public cloud, per user** (`principal_propagation`):

1. BTP subaccount → Connectivity → Destination Trust → Generate/Export the signing certificate.
2. S/4HANA Cloud → Communication Systems: system for the BTP subaccount with **SAML Bearer Assertion
   Provider = ON**, the exported certificate, issuer = its subject CN; inbound user with **OAuth 2.0**;
   communication arrangement for `API_JOURNALENTRYITEMBASIC_SRV` (and the optional services) using it,
   on a scenario that allows OAuth 2.0 (`SAP_COM_0241`, not `SAP_COM_0087`; see above).
3. BTP destination: Type HTTP, URL `https://<tenant>-api.s4hana.cloud.sap`, Proxy Type Internet,
   Authentication **OAuth2SAMLBearerAssertion**, Audience = S/4HANA's SAML2 audience, Client Key = the
   communication user, Token Service URL `https://<tenant>-api.s4hana.cloud.sap/sap/bc/sec/oauth2/token`,
   Token Service User/Password = that user, Name ID Format e-mail (`urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress`),
   `scope` = the arrangement's OAuth scopes.
4. Each user needs a business user in S/4HANA with the same e-mail and the business roles that the
   APIs check.
5. MCP clients sign in with the subaccount's XSUAA (hop 1 OIDC with that issuer): the Destination service
   only accepts user tokens its subaccount trusts.

**Private cloud / on-premise, per user**: destination Proxy Type **OnPremise**, Authentication
**PrincipalPropagation**, URL = Cloud Connector virtual host, which **must be `http://`** (the connectivity
credentials are proxy headers, and for an `https://` URL they would be tunnelled to the backend instead;
TLS to the backend is configured in Cloud Connector), `sap-client`; Cloud Connector principal propagation +
STRUST + CERTRULE as described above. Bind the Connectivity service (`VCAP_SERVICES`; its `token_service_url`
is the UAA base and `/oauth/token` is added, per SAP's
[Consuming the Connectivity Service](https://help.sap.com/docs/r/cca91383641e40ffbe03bdc78f00f681/Cloud/en-US/313b215066a8400db461b311e01bd99b.html))
or set `S4HC_CONNECTIVITY_PROXY`, `S4HC_CONNECTIVITY_TOKEN_URL`, `S4HC_CONNECTIVITY_CLIENT_ID`,
`S4HC_CONNECTIVITY_CLIENT_SECRET`. Only the client-secret binding type is supported (not the X.509 one). **The connectivity proxy is only reachable
from inside BTP Cloud Foundry or Kyma**, so this mode means deploying S4HC_MCP there, not on GenAI Studio's
own cluster. The server exchanges the user token at the connectivity service (jwt-bearer) and sends it as
`Proxy-Authorization`, SAP's recommended variant.

`destination` (no user) reads a technical destination (`BasicAuthentication`, `OAuth2ClientCredentials`,
…) instead of `S4HC_*` credentials — useful to keep SAP passwords in BTP rather than in this deployment.
The destination is looked up again when its credentials expire (at most every 5 minutes), so a password
rotated in BTP is picked up without a restart.

### Hop 2 — `user_token` (S/4HANA sign-in, no BTP)

The CALM_MCP pattern: the MCP client signs the user in at S/4HANA's OAuth server (authorization code) and
sends the S/4HANA token as `Authorization: Bearer`. This server publishes RFC 9728 metadata pointing at
itself and an RFC 8414 document describing S/4HANA's endpoints (S/4HANA publishes none), checks each new
token with one `$metadata` read, then forwards it. These checks happen before the caller is
authenticated, so every path to SAP is bounded. A token SAP has not seen yet is checked at most 8 at a time,
`S4HC_SAP_TOKEN_CHECKS_PER_SOURCE_PER_MIN` (default 20) per sender and `S4HC_SAP_TOKEN_CHECKS_PER_MIN`
(default 60) in all; over that it gets `401` without SAP being asked, and a refused token is remembered
for 1 minute. The sender is the peer address, or, behind N trusted proxies (`S4HC_TRUSTED_PROXY_HOPS=N`,
default 0), the `X-Forwarded-For` entry the outermost of them appended; entries left of it are set by the
caller and ignored. **Behind an ingress, set `S4HC_TRUSTED_PROXY_HOPS`** to the number of proxies that
append `X-Forwarded-For`: otherwise every caller is the ingress, one sender, and anyone can use up that one
share for everyone. With it set, one sender cannot block the others; a flood from many senders can still
delay sign-ins and token refreshes (a refreshed token is a new token), and with the defaults three senders
use up the global budget. All users of one MCP client platform (GenAI Studio's egress, say) are one sender,
so size `S4HC_SAP_TOKEN_CHECKS_PER_SOURCE_PER_MIN` for their peak sign-ins and refreshes per minute. A token SAP has accepted is trusted
for 5 minutes, then re-checked outside that budget, one check per token at a time; while that check is
running or SAP does not answer (retried after 30 s), the token stays valid for up to 10 minutes after SAP
last confirmed it. Only SAP refusing it revokes it. Each token is its own MCP session principal, so a
session opened by one user cannot be used with another user's token. S/4HANA tokens are opaque, so this
server cannot read the user's name (the audit log shows `caller=sap-token:<hash>`); S/4HANA's own logs show
it. Cannot be combined with OIDC.

**Set up on S/4HANA Cloud** (as done on the Syntax Gamma tenant, 2026-11-05; needs an administrator with
the communication management apps):

1. *Communication Systems* → New: inbound only (no host name needed). Under **OAuth 2.0 Client Settings**
   add the MCP client's redirect: for a desktop client choose **Loopback** with path `/oauth/callback`
   (any port is accepted, so `http://127.0.0.1:<port>/oauth/callback` works).
2. In the same system, *Users for Inbound Communication* → add a user with authentication method
   **OAuth 2.0**, a public client (**Authorization Code + PKCE**, no secret). Its name is the OAuth client ID.
3. *Communication Arrangements* → New, on a scenario that allows OAuth 2.0 (`SAP_COM_0241`, see above),
   with that system and inbound user. Set the outbound services the scenario lists to inactive if it asks
   for an outbound user.
4. Open the arrangement's **OAuth 2.0 Details**: it shows the client ID, the authorization and token
   endpoints and the scopes (one per service, `<SERVICE>_0001`).
5. Each person who signs in uses their own S/4HANA login and needs the business roles the APIs check;
   SAP refuses what their roles do not allow (`SAP_FORBIDDEN`), whatever the arrangement contains.

**Set on this server:** `S4HC_SAP_AUTH=user_token`, `S4HC_PUBLIC_URL`, `S4HC_SAP_OAUTH_SCOPES` (the scopes
from step 4, space or comma separated). Optional: `S4HC_SAP_OAUTH_AUTHORIZE_URL` (default
`https://<tenant>.s4hana.cloud.sap/sap/bc/sec/oauth2/authorize`, the UI host), `S4HC_SAP_OAUTH_TOKEN_URL`
(default `https://<tenant>-api.s4hana.cloud.sap/sap/public/bc/sec/oauth2/token`, the public-client token
endpoint the arrangement shows; `S4HC_OAUTH_TOKEN_URL` is read when it is unset), `S4HC_SAP_OAUTH_PKCE`
(default true). While PKCE is on, the metadata lists `none` as a token endpoint auth method, so a client registered as a
public client sends no secret. Keep `S4HC_USER` / `S4HC_PASSWORD` and add `S4HC_MIXED_AUTH=true` and
`S4HC_MCP_API_KEY` to keep the API key working beside the sign-in.

**Try it with the MCP Inspector** (`npx -y @modelcontextprotocol/inspector@2.12.0` with
`MCP_INSPECTOR_SECRET_STORE=memory`; the default keychain store drops custom header values): open it at
`http://127.0.0.1:6274`, not `localhost`, so its redirect `http://127.0.0.1:6274/oauth/callback` matches the
loopback rule; add `http://127.0.0.1:6274` to `S4HC_ALLOWED_ORIGINS`. Connect to `<S4HC_PUBLIC_URL>/mcp` with
Streamable HTTP, enter the client ID, untick **Request refresh token** (the scopes above do not include
`offline_access`), and sign in on the tenant's own login page. A client a hosted MCP platform registers
(GenAI Studio, say) needs its own redirect URL in step 1; that variant is not verified yet.

## Verified vs. not verified

| Claim | Status |
|---|---|
| OIDC resource server, 401 challenge, RFC 9728 metadata, signature/issuer/audience/expiry checks | Unit + HTTP end-to-end tests; live against Gamma with locally signed tokens (2026-11-04) |
| Gamma's `/sap/bc/sec/oauth2/token` and `/authorize` exist; password-only user is not an OAuth client | Live probe (2026-11-04) |
| Destination service parsing (all four ARC-1 credential shapes), PP public + private, strict refusal, per-user cache isolation | Unit tests against a faked Destination/Connectivity service |
| S/4HANA sign-in (`user_token`): authorization code + PKCE with a public client on a `SAP_COM_0241` arrangement, through the MCP Inspector, with `S4HC_MIXED_AUTH` | **Verified live on Gamma (2026-11-05):** the signed-in user and the API key (communication user) got the same company code total at the same time, and only the signed-in user could read G/L line items (the communication user got `SAP_FORBIDDEN`), so SAP saw two different identities |
| `SAP_COM_0087` cannot use OAuth 2.0 (Basic and certificate only) | Seen on Gamma's arrangement page (2026-11-05) |
| A real BTP subaccount issuing per-user S/4HANA tokens for these OData APIs | **Not verified** — needs a BTP subaccount, the tenant trust setup and an arrangement on an OAuth-capable scenario |
| A hosted MCP platform's redirect URL (not loopback) in the OAuth 2.0 client settings | **Not verified** |
| `client_credentials` grant on S/4HANA Cloud for OData arrangements | **Not verified** — needs an OAuth-enabled communication user |
