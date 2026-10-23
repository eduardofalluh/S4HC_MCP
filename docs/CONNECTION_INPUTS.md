# What someone needs to connect to the S/4HANA Cloud MCP

Compared with CALM MCP (8 inputs, 4 optional), this one is simpler because
S/4HANA Cloud Public Edition uses a **communication user** directly on the API
host — there is no separate XSUAA token URL, identity zone or region zone.

## Per-connection inputs: 3 required, 5 optional

| # | Input | Env var (hosted) | Header (per connection, GenAI Studio) | Required | Where to get it / what it does |
|---|---|---|---|---|---|
| 1 | API host | `S4HC_BASE_URL` | `x-s4hc-base-url` | **Yes** | `https://<tenant>-api.s4hana.cloud.sap` — the `-api` host, not the Fiori URL. Shown in *Communication Systems* → *Own System*. |
| 2 | Communication user | `S4HC_USER` | `x-s4hc-user` | **Yes** | Fiori app *Maintain Communication Users*. One per connecting system is SAP's recommendation. |
| 3 | Password | `S4HC_PASSWORD` | `x-s4hc-password` | **Yes** | Set when creating the user. Alternative to 2+3: X.509 client certificate via `S4HC_CLIENT_CERT`/`S4HC_CLIENT_KEY` (file paths) or `S4HC_CLIENT_CERT_PEM`/`S4HC_CLIENT_KEY_PEM` (inline). |
| 4 | Ledger | `S4HC_LEDGER` | `x-s4hc-ledger` | No (default `0L`) | Leading ledger is `0L` on every Public Cloud tenant; `2L`/`3L` for parallel ledgers. |
| 5 | Language | `S4HC_LANGUAGE` | `x-s4hc-language` | No (default `EN`) | Language for cost center names (`EN`, `FR`, `PT`…). |
| 6 | Controlling area | `S4HC_CONTROLLING_AREA` | `x-s4hc-controlling-area` | No | Pre-filters every query to one controlling area (e.g. `A000`). |
| 7 | Allowed company codes | `S4HC_ALLOWED_COMPANY_CODES` | `x-s4hc-allowed-company-codes` | No | Comma list. Backend-enforced data scope: queries are restricted to these, and a request for any other code is refused. When both are set, the header can only narrow the env list; a header that lists nothing (e.g. `,`) is refused, never read as "unrestricted". |
| 8 | Allowed cost centers | `S4HC_ALLOWED_COST_CENTERS` | `x-s4hc-allowed-cost-centers` | No | Comma list. Same, for cost centers. Lets one deployment serve a controller who may only see their own area. Lists over 200 entries are applied to the rows after the query instead of in the `$filter`. |

Header credentials are all-or-nothing: `x-s4hc-user` and `x-s4hc-password` must come together, and an
`x-s4hc-base-url` other than the server's own `S4HC_BASE_URL` must bring both. The server never lends its own
credentials to a host named in a request, and only accepts API hosts ending in `S4HC_ALLOWED_HOST_SUFFIXES`
(default `.s4hana.cloud.sap,.s4hana.ondemand.com`).

Plus, like CALM, the optional `x-user-email` header (`{{user.email}}` in GenAI
Studio) is recorded as the caller in the audit log.

**Deployment-level (set once on the server, not per connection):**

| Secret / flag | Default | Purpose |
|---|---|---|
| `S4HC_MCP_API_KEY` | unset | When set, every call to `/mcp` must send `Authorization: Bearer <key>` (or `x-api-key`). Protects the AI-client → MCP hop. Without it, `--http` refuses to bind a non-loopback host. |
| `S4HC_MCP_ALLOW_UNAUTHENTICATED` | `false` | Set `true` only when the platform in front authenticates every caller and you deliberately run without the key. |
| `S4HC_ALLOWED_ORIGINS` | unset | Browser origins allowed to call `/mcp`. Any request carrying another `Origin` header gets 403 (DNS-rebinding guard); server-side MCP clients send none. |
| `S4HC_EXPOSE_PERSON_FIELDS` | `false` | Responsible-person names and user IDs (`CostCtrResponsiblePersonName`, `ProfitCtrResponsibleUser`, `AccountingDocCreatedByUser`, …) are personal data sent to the LLM provider. They are never selected by default and can only be requested via `fields` when this is `true`. |
| `S4HC_ALLOWED_HOST_SUFFIXES` | SAP API domains | Hosts `S4HC_BASE_URL` / `x-s4hc-base-url` may point at. |
| `S4HC_ENABLE_RAW_ODATA` | `false` | Enables the admin-only `s4hc_odata_read` escape hatch. Leave off in production. |
| `S4HC_JOURNAL_ITEM_SERVICE`, `S4HC_COSTCENTER_SERVICE`, `S4HC_PROFITCENTER_SERVICE`, `S4HC_GLACCOUNT_SERVICE`, `S4HC_GLLINEITEM_SERVICE`, `S4HC_PLAN_SERVICE` | SAP standard paths | Override with the inbound path shown in the tenant's Communication Arrangement if it differs. Never guess by appending `_SRV`. |

Server-side knobs that are **not** per-connection inputs: `S4HC_RATE_LIMIT_PER_MIN`
(120, per connection), `S4HC_RATE_LIMIT_GLOBAL_PER_MIN` (600, shared by trusted connections: the server's
own credentials and any credential SAP has answered 2xx for), `S4HC_RATE_LIMIT_UNVERIFIED_PER_MIN` (60, shared
by all credentials SAP has not accepted yet, so invented ones cannot crowd out real tenants), `S4HC_PAGE_SIZE` (1000), `S4HC_MAX_ROWS`
(50000), `S4HC_MAX_INLINE_ROWS` (5000 raw rows per response), `S4HC_MAX_PAGES` (100),
`S4HC_REQUEST_DEADLINE_SECONDS` (120, one paged read including retries), `S4HC_MASTER_CACHE_TTL` (600 s),
`S4HC_MAX_CACHED_CONNECTIONS` (64), `S4HC_MAX_CACHED_RESULTS` (512), `MCP_HOST`, `MCP_PORT`,
`MCP_TRUST_PROXY`, `LOG_LEVEL`.

## Tenant-side prerequisites (once per tenant, key user / Basis)

1. **Maintain Communication Users** → create the user (password or certificate).
2. **Maintain Communication Systems** → create a system for "Syntax GenAI Studio", add the user under *Users for Inbound Communication*.
3. **Maintain Communication Arrangements** → create the scenarios for the services you want. Scenario IDs below are the documented candidates; the authoritative mapping is what the tenant's arrangement app shows for its release.

| Service | Enables | Scenario (confirm in tenant) | Need |
|---|---|---|---|
| `API_JOURNALENTRYITEMBASIC_SRV` | Actuals + company codes, cost/profit centers, G/L accounts (value helps in the same service) | `SAP_COM_0087` (Financial Planning Integration) or `SAP_COM_0241` | **Required** |
| `API_COSTCENTER_SRV` | Cost center names/descriptions in `list_cost_centers` | `SAP_COM_0087` per partner guides; newer releases ship a V4 cost center API (`SAP_COM_0943` lineage) — if only V4 is available, set `S4HC_COSTCENTER_SERVICE` or rely on the fallback | Optional |
| `API_PROFITCENTER_SRV` | Profit center names and the profit center ↔ company code assignment in `list_profit_centers` (without it the assignment is derived from cost centers) | Confirm in tenant | Optional |
| `API_GLACCOUNTINCHARTOFACCOUNTS_SRV` | G/L account names in `list_gl_accounts` | Confirm in tenant | Optional |
| `API_FINPLANNINGENTRYITEM_SRV` | `get_actual_vs_plan` | `SAP_COM_0087` | Optional |
| `API_GLACCOUNTLINEITEM` | `get_cost_center_postings` (document-level detail) | `SAP_COM_0303` | Optional |

   If `SAP_COM_0087` is used, a **single** arrangement typically covers actuals, cost centers and plan.
4. Copy the inbound service URLs from each arrangement. If a path differs from the defaults, set the matching `S4HC_*_SERVICE` override.
5. Verify with `s4hc_health(live=true)` → `ok` (all six) or `ok_with_optional_services_missing`. Each service is checked twice: `$metadata` and a one-row data read, because a user can be allowed the first and not the second ("You do not have start authorization for R3TR IWSV …"). The result names what each missing service would enable and echoes the data scope in effect.
6. Run `python scripts/live_smoke.py` with the same settings: it exercises all 16 tools and exits non-zero on any failure.

The communication user has only the authorizations those arrangements grant;
the MCP only issues HTTP GET, so nothing can be changed in the tenant.

## Compliance mapping (SAP API Policy v.4.2026a + ref-arch 137800)

SAP's API Policy §2.2.2 prohibits generative-AI systems that plan/select/execute
API calls **"except through and within the limits of SAP-endorsed architectures"**.
SAP's Architecture Center reference *Third-Party MCP Server Access to SAP Solutions*
(ref-arch 137800, updated June 2026) is such an architecture: it allows an external
or custom MCP server provided its conditions are met. How this server meets them:

| Ref-arch condition | How S4HC_MCP meets it |
|---|---|
| API Policy adhered to, incl. General API Controls (§2.1) | Published APIs only, used for their documented purpose (reporting reads); paging with a hard row ceiling; no bulk replication — aggregation is pushed to SAP. |
| Authentication on every connection | Communication user (Basic or X.509) per tenant; no anonymous path. Per-connection headers mean no shared credential across tenants; header and server credentials are never mixed, and cached clients/results are keyed on a credential fingerprint. |
| Never store long-lived credentials in the server | Credentials come from the platform secret store (env) or per-request headers; nothing on disk, nothing logged. |
| Rate limits of the underlying API respected | Per-connection and global limits at the MCP layer (unverified credentials capped separately); client honours `Retry-After` on 429/503 with backoff; response bodies streamed with a 64 MB cap. |
| Input validation, TLS 1.2+ | Every ID / field name validated against an allowlist; https enforced on the base URL, which must be a plain origin on an SAP API domain; redirects are never followed. |
| Authorization enforced | Backend data scope (allowed company codes / cost centers) — the agent cannot widen it. |
| Monitoring | Audit log per tool call: tool, caller, scope args, duration, outcome. |
| No systematic / large-scale extraction (§2.2.2 b) | Aggregation is pushed to SAP; totals that would exceed `S4HC_MAX_ROWS` are **refused** (`QUERY_TOO_LARGE`), not split into many calls; postings are capped at 2000 rows per call; page cap `S4HC_MAX_PAGES`. |
| Pagination safety | Continuation links followed only on the configured origin + service path, never twice; otherwise `INCOMPLETE_RESULT`. |
| Inbound caller auth | `S4HC_MCP_API_KEY` bearer on `/mcp` (required to bind publicly unless explicitly waived); browser `Origin` guard; full OAuth via platform proxy or Integration Suite MCP Gateway. |
| Data minimisation (GDPR) | Personal fields (responsible persons, user IDs) are opt-in per deployment (`S4HC_EXPOSE_PERSON_FIELDS`) and never in a default selection. |
| No raw query surface for the agent | `s4hc_odata_read` disabled by default and refused under any data scope. |

**Known gap to state honestly:** the ref-arch prefers user-context propagation
(OAuth 2.0 token exchange so each call carries the end user's SAP authorizations).
Communication arrangements on Public Cloud are system-to-system, so this server
runs under the communication user and enforces per-user scope itself via the
allowlists. SAP's own path to true principal propagation is the Integration Suite
MCP Gateway (OAuth2SAMLBearer token exchange — on SAP's roadmap, not GA as of
July 2026). If a client requires that, front these same tools with the Gateway.

Licensing: SAP lists indirect/digital access as a licensing consideration. Read-only
use does not create documents, but that does not by itself settle a given
customer's contract — confirm with the customer's SAP licensing contact before production.

## Financial correctness rules built in

- Cost center tools count only journal lines posted to a cost center (`CostCenter ne ''`). Without that, balance sheet and revenue lines would make every company code total ≈ 0. Pass `cost_center_lines_only=false` to `get_cost_center_actuals`, `actuals_breakdown`, `export_actuals_csv` or `get_actual_vs_plan` for a whole-ledger view.
- Amounts are exact decimals end to end; never floats.
- Totals across **different currencies are refused** (`MIXED_CURRENCY`), also across the two years or ranges a comparison combines; filter to one company code or use `currency='global'`.
- `FiscalPeriod` is normalised to three digits (SAP returns `'1'`), so periods sort and match.
- A total that would be computed over a **truncated** result is refused (`QUERY_TOO_LARGE`); listing tools mark `completeness.status = partial` instead.
- Variance % uses |baseline|; zero baseline → `null`.
- Period 000 (carryforward) and 013–016 (special periods) are accepted explicitly; default range is 001–012.
- `fiscal_context(company_code=…)` warns when the fiscal year variant is not K4 (period ≠ month).
- Driver contributions in `compare_cost_center_periods` reconcile exactly to the total change.
- `cost_center_trend` lists every period in the range (0 where nothing was posted), so month-over-month always compares adjacent periods; a period is an anomaly only when both its deviation from the average (`anomaly_threshold_pct`) and its z-score (`anomaly_z`) are large, and only with 4+ periods.
- Anomaly flags are descriptive deviations, not findings of error.
- `get_cost_center_postings` always filters on the ledger (default `0L`), so parallel ledgers do not double-count.
- Every analytical result carries `provenance` (source API, retrieved_at) and `completeness` (status, warnings).

## Before production (tenant gates — cannot be done from code)

1. Joe's current export/report (sanitized) and its columns → reconcile at least two periods × several cost centers against `cost_center_summary` / `get_cost_center_actuals`, agreeing ledger, currency basis, allocations and reversals.
2. Tenant release, inbound URLs and `$metadata` for each service (`describe_service`) → confirm field names match; save EDMX.
3. Which planning category holds the budget (or confirm budgets live only in SAC → plan tool stays off).
4. Access rules: which company codes / cost centers each audience may see → set the allowlists.
5. Expected volume / refresh frequency → tune `S4HC_RATE_LIMIT_PER_MIN`, `S4HC_MAX_ROWS`.
6. Licensing / data-processing sign-off (incl. AI-provider data retention).

## Claude Desktop (stdio) example

```json
{
  "mcpServers": {
    "sap-s4hana-cloud": {
      "command": "/path/to/S4HC_MCP/venv/bin/python",
      "args": ["/path/to/S4HC_MCP/server.py"],
      "env": {
        "S4HC_BASE_URL": "https://<tenant>-api.s4hana.cloud.sap",
        "S4HC_USER": "<communication user>",
        "S4HC_PASSWORD": "<password>",
        "S4HC_LEDGER": "0L",
        "S4HC_LANGUAGE": "EN"
      }
    }
  }
}
```

## GenAI Studio (HTTP, multi-tenant) example

Deploy once with **no** `S4HC_*` credentials; each agent connection injects:

```
x-s4hc-base-url:               https://<tenant>-api.s4hana.cloud.sap   (required)
x-s4hc-user:                   <communication user>                     (required)
x-s4hc-password:               <password>                               (required)
x-s4hc-ledger:                 0L                                        (optional)
x-s4hc-language:               EN                                        (optional)
x-s4hc-controlling-area:       A000                                      (optional)
x-s4hc-allowed-company-codes:  1010,1710                                 (optional)
x-s4hc-allowed-cost-centers:   4100,4120,4130                            (optional)
x-user-email:                  {{user.email}}                            (optional, audit)
Authorization:                 Bearer <S4HC_MCP_API_KEY>                 (if the deployment sets one)
```

Single-tenant alternative: set `S4HC_*` as deployment secrets and leave the headers out.
