# S4HC_MCP

Read-only MCP server that exposes SAP S/4HANA Cloud (Public Edition) cost
center master data and actuals (with optional plan and G/L posting detail) as
16 tools, so a GenAI Studio / Claude agent can query a tenant directly over
OData instead of someone exporting reports and pasting them in. It talks to
the tenant's API host (`https://<tenant>-api.s4hana.cloud.sap`) using a
communication user (Basic auth or X.509 cert). It runs over stdio for
Claude Desktop / Cursor / MCP Inspector, or as a Streamable HTTP service
(`python server.py --http`) behind Syntax's GenAI Studio, same deployment
shape as the sibling `CALM_MCP` repo.

## Commands

- Install: `python -m venv venv && source venv/bin/activate && pip install -r requirements.txt`
- Test: `python -m pytest tests -q` (57 tests: unit tests against a fake OData backend in `tests/fake_s4hc.py`, plus HTTP end-to-end tests in `tests/test_http_e2e.py` that spawn the real server process)
- Lint and type-check: none configured (no ruff/flake8/mypy/pyproject in the repo)
- Run locally: `cp .env.example .env` (fill in `S4HC_BASE_URL` + credentials), then `python server.py` (stdio) or `python server.py --http --port 8000` (Streamable HTTP at `/mcp`)

## Architecture

- `server.py` — entrypoint; builds the `FastMCP` app, registers every tool module, adds the audit/rate-limit middleware and the optional inbound-API-key / trust-proxy HTTP middleware, parses `--http/--host/--port`.
- `src/s4hc/config.py` — all configuration from env vars (no hard-coded tenant); also the field allowlists for each OData entity (`A_CostCenter`, `A_JournalEntryItemBasic`, `A_FinPlanningEntryItem`, `A_CompanyCode`, `A_ProfitCenter`, `A_GLAccountInChartOfAccounts`), verified against SAP's generated Cloud SDK clients.
- `src/s4hc/client.py` — `S4HCClient`: GET-only OData V2 client. Handles Basic/X.509 auth, `$top/$skip` + `__next` paging restricted to the configured origin/service path, retry with backoff on 429/5xx honouring `Retry-After`, `/Date()/` → ISO normalization, `S4HCError` with stable machine-readable codes (`SAP_UNAUTHORIZED`, `QUERY_TOO_LARGE`, `UNAUTHORIZED_SCOPE`, `PLAN_UNAVAILABLE`, …).
- `src/s4hc/dependencies.py` — resolves a client and request defaults (ledger/language/controlling area) per call: per-request `x-s4hc-*` headers first (multi-tenant HTTP deployments), then env vars. Also the small TTL cache for master data, partitioned by tenant+user+scope.
- `src/s4hc/governance.py` — `AuditAndRateLimitMiddleware` (audit log + `S4HC_RATE_LIMIT_PER_MIN` token bucket) and the backend data-scope allowlist (`S4HC_ALLOWED_COMPANY_CODES` / `S4HC_ALLOWED_COST_CENTERS` or per-request headers) that every tool enforces before querying.
- `src/s4hc/tools/` — one module per tool group, each with a `register(mcp)` called from `server.py`: `health` (`s4hc_health`), `master_data` (company codes, profit centers, G/L accounts), `cost_centers` (`list_cost_centers`), `actuals` (`get_cost_center_actuals`, `cost_center_summary` — shared helpers like `fetch_actuals`/`totals_by`/`envelope` live here and are imported by `analysis.py` and `plan.py`), `analysis` (breakdowns, period comparison, trend, CSV export, fiscal context), `postings` (G/L line-item detail, validates fields against live `$metadata` since no SDK client exists for that service), `plan` (actual vs. plan), `odata_read` (`describe_service` + the admin-only `s4hc_odata_read` escape hatch, gated by `S4HC_ENABLE_RAW_ODATA`).
- Start reading at `server.py` to see the tool inventory, then `src/s4hc/tools/actuals.py` — it defines the shared aggregation/formatting helpers most other tool modules build on.
- `tests/fake_s4hc.py` is a fake OData backend (monkeypatches `requests.Session.get`) driving the unit tests in `tests/test_server.py`; `tests/conftest.py` wires it in and generates throwaway test credentials at import time.
- `docs/CONNECTION_INPUTS.md` and `docs/GENAI_STUDIO_DEPLOYMENT.md` cover tenant setup/compliance mapping and the HTTP deployment pattern respectively.

## Conventions

- Every OData entity field used anywhere must appear in the corresponding `*_FIELDS` / `*_DEFAULT_SELECT` list in `config.py`; `validate_fields()` in `client.py` rejects anything else with `UNSUPPORTED_FIELD` rather than passing it through to SAP.
- All IDs (company code, cost center, etc.) and raw OData string values go through `validate_id()` / `odata_str()` in `client.py` before being interpolated into a `$filter` — never build filter strings by hand.
- New tools must call the governance helpers (`enforce_company_code`, `enforce_cost_centers`, `allowed_company_codes_for_filter`, `filter_rows_to_scope`) so the backend data-scope allowlist is actually applied, not just documented.
- The client is strictly GET-only; there is no write path anywhere in `src/s4hc`, and that's intentional (see README "Extending").
- Field names for `A_CostCenter`/`A_JournalEntryItemBasic`/`A_FinPlanningEntryItem` are pinned against specific `@sap/cloud-sdk-vdm-*` package versions (noted in `config.py` comments); if SAP changes these entities, re-verify against the SDK before editing the lists. `postings.py` (`API_GLACCOUNTLINEITEM`) has no such SDK reference, so it validates fields against the tenant's live `$metadata` instead.

## Gotchas

- `A_JournalEntryItemBasic` and `A_FinPlanningEntryItem` are *aggregating* entities — SAP sums measures server-side over whatever dimensions are in `$select`. Adding a field to a select list changes what gets grouped, not just what's returned.
- Two entirely separate code paths resolve the same OData client: env vars (single-tenant) and `x-s4hc-*` request headers (multi-tenant GenAI Studio). A bug fixed in one path can still be present in the other — `dependencies.py` is where both are resolved.
- `S4HC_MCP_API_KEY`, `S4HC_ALLOWED_COMPANY_CODES`, etc. can come from env *or* per-request headers; headers always win. Don't assume the env var is authoritative when reasoning about scope.
- `tests/conftest.py` sets `S4HC_PAGE_SIZE=2` to force paging/`__next` logic to actually exercise during the unit tests — don't "fix" small page counts you see in test fixtures.
- `tests/test_http_e2e.py` spawns a real `server.py` subprocess and talks to it over HTTP with the fake backend injected via `sitecustomize`; it needs a free local port and will fail outside a normal local Python environment (e.g. sandboxed or offline CI without loopback sockets).
- `postings.py` can't assume SAP's standard property names for `API_GLACCOUNTLINEITEM` — it probes `$metadata` at first use and fails with a specific missing-field message rather than guessing, which is slower on cold start than the other tools.
