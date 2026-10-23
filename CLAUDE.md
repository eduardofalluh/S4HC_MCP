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
- Test: `python -m pytest tests -q` (112 tests: unit tests against a fake OData backend in `tests/fake_s4hc.py`, the review regression tests in `tests/test_review_fixes.py`, plus HTTP end-to-end tests in `tests/test_http_e2e.py` that spawn the real server process)
- Live check: `python scripts/live_smoke.py` runs all 16 tools against the tenant in `S4HC_*` (read-only; prints counts and totals, never rows or credentials)
- Lint and type-check: none configured (no ruff/flake8/mypy/pyproject in the repo)
- Run locally: `cp .env.example .env` (fill in `S4HC_BASE_URL` + credentials), then `python server.py` (stdio) or `python server.py --http --port 8000` (Streamable HTTP at `/mcp`)

## Architecture

- `server.py` — entrypoint; loads `.env` before any `src.s4hc` import, builds the `FastMCP` app, registers every tool module, adds the audit/rate-limit middleware and the Origin guard / inbound-API-key / trust-proxy HTTP middleware, parses `--http/--host/--port`, and refuses a public bind without `S4HC_MCP_API_KEY`.
- `src/s4hc/errors.py` — `S4HCError` with stable machine-readable codes (`NOT_CONFIGURED`, `SAP_FORBIDDEN`, `QUERY_TOO_LARGE`, `MIXED_CURRENCY`, `UNAUTHORIZED_SCOPE`, `PLAN_UNAVAILABLE`, …); re-exported by `client.py`.
- `src/s4hc/config.py` — all configuration from env vars (no hard-coded tenant): service paths, field allowlists per entity, `NAME_FOR` (dimension → its name field), `PERSON_FIELDS`, base-URL validation (plain https origin on an SAP API domain), the inline-PEM cert cache.
- `src/s4hc/client.py` — `S4HCClient`: GET-only OData V2 client. Basic/X.509 auth, never follows redirects, follows `__next` whenever present (else `$top/$skip`), asks for one row past the ceiling so `truncated` is exact, retry with backoff on 429/5xx honouring `Retry-After` (seconds or HTTP-date), one deadline per paged read, `/Date()/` → ISO.
- `src/s4hc/dependencies.py` — resolves a `Connection` per call: header credentials (all three `x-s4hc-*` together) or the server's env credentials, never mixed. Client cache and master-data cache are LRU-bounded and keyed on a credential fingerprint plus the data scope.
- `src/s4hc/governance.py` — data-scope allowlists (env is a ceiling headers can only narrow; row post-filter fails closed), audit log, per-connection + global rate limit.
- `src/s4hc/tools/` — one module per tool group, each with a `register(mcp)` called from `server.py`: `health` (`s4hc_health`: `$metadata` + one-row read per service), `master_data` (company codes, profit centers with names and company code assignment, G/L accounts with names), `cost_centers`, `actuals` (`get_cost_center_actuals`, `cost_center_summary` — shared helpers `fetch_actuals`/`build_filter`/`envelope`/`ensure_single_currency`/`pad_period` live here), `analysis` (breakdowns, period comparison, trend, CSV export, fiscal context), `postings` (G/L line items, fields validated against live `$metadata`), `plan` (actual vs. plan), `odata_read` (`describe_service` + the admin-only `s4hc_odata_read`).
- Start reading at `server.py`, then `src/s4hc/tools/actuals.py`.
- `tests/fake_s4hc.py` mimics a real tenant (unpadded `FiscalPeriod`, blank profit-center `CompanyCode`, payables lines without cost center that net the ledger to zero, `__metadata`/`__deferred` on rows, `ne` filters, server-driven `__next` paging, metadata readable while data is 403). Keep it that way: every fake simplification hid a real bug once.

## Conventions

- Every OData entity field used anywhere must appear in the corresponding `*_FIELDS` / `*_DEFAULT_SELECT` list in `config.py`; `validate_fields()` rejects anything else with `UNSUPPORTED_FIELD`. A dimension with a name field goes into `NAME_FOR` too, and both must be in `JOURNAL_ITEM_DIMENSIONS`.
- All IDs and raw OData string values go through `validate_id()` / `odata_str()` before being interpolated into a `$filter` — never build filter strings by hand.
- New tools must call the governance helpers (`enforce_company_code`, `enforce_cost_centers`, `allowed_company_codes_for_filter`, `scope_columns` + `filter_rows_to_scope`) so the data scope is applied, not just documented.
- Cost center tools pass through `fetch_actuals`, which adds `CostCenter ne ''` unless `cost_center_lines_only=False`; any tool that sums across two pulls calls `ensure_single_currency` over both.
- Personal data (`config.PERSON_FIELDS`) is never in a default select and only selectable with `S4HC_EXPOSE_PERSON_FIELDS=true`.
- The client is strictly GET-only; there is no write path anywhere in `src/s4hc`, and that's intentional.

## Gotchas

- `A_JournalEntryItemBasic` and `A_FinPlanningEntryItem` are *aggregating* entities — SAP sums measures over whatever dimensions are in `$select`. They also hold every ledger line: without the cost center filter, a company code's total is ~0.
- SAP returns `FiscalPeriod` and `LedgerFiscalYear` without leading zeros (`display-format="NonNegative"`); filters accept `'001'` or `'1'`. Use `pad_period` on anything that sorts or keys on periods.
- The profit center master's `CompanyCode` is blank on a real tenant; the assignment lives in `API_PROFITCENTER_SRV/A_PrftCtrCompanyCodeAssignment` (fallback: derive it from cost centers).
- A communication user can read a service's `$metadata` but still get 403 "no start authorization" on its data — health must probe data, not only metadata.
- `S4HC_BASE_URL` is the `-api` host. The Fiori host answers with a redirect, which the client refuses with `NOT_CONFIGURED`.
- Two code paths resolve the same client: env vars (single-tenant) and `x-s4hc-*` headers (multi-tenant). A header base URL other than the env one must bring header credentials.
- `tests/conftest.py` sets `S4HC_PAGE_SIZE=2` to force paging, generates throwaway credentials per run, and blocks any non-loopback socket connection.
- In a Claude Code sandbox that denies `**/*credentials*`, `fastmcp/client/auth/client_credentials.py` is unreadable, so tests that import `fastmcp.Client` fail at import; run the suite outside that sandbox.
- `tests/test_http_e2e.py` spawns a real `server.py` subprocess on a free loopback port with the fake backend injected via `sitecustomize`.
