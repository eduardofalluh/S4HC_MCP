# SAP S/4HANA Cloud (Public Edition) MCP — Cost Center Analysis

Read-only MCP server that lets a GenAI Studio / Claude agent pull cost center
master data and actuals straight from an S/4HANA Cloud Public Edition tenant,
instead of downloading reports and uploading them to the agent.

Same shape as `CALM_MCP`: FastMCP + `requests`, stdio or Streamable HTTP,
env-var or per-request-header credentials, `src/s4hc/{config,client,dependencies,tools}`.

## Tools (16, all read-only)

See `docs/GENAI_STUDIO_DEPLOYMENT.md` for the full table. Core: `cost_center_summary`,
`compare_cost_center_periods`, `actuals_breakdown`, `cost_center_trend`,
`get_cost_center_actuals`, `get_cost_center_postings`, `get_actual_vs_plan`,
master data (`list_company_codes`, `list_cost_centers`, `list_profit_centers`,
`list_gl_accounts`), plus `s4hc_health`, `fiscal_context`, `export_actuals_csv`,
`describe_service` and the admin-only `s4hc_odata_read`.

`A_JournalEntryItemBasic` is an aggregating entity: amounts are summed over the
dimensions in `$select`, so grouping happens inside S/4HANA and row counts stay small.
The same service ships `A_CompanyCode`, `A_CostCenter`, `A_ProfitCenter` and
`A_GLAccountInChartOfAccounts`, so **one** communication arrangement covers master data and actuals.
Field names were verified against SAP's generated clients
(`@sap/cloud-sdk-vdm-cost-center-service` 2.1.0 and
`@sap/cloud-sdk-vdm-journal-entry-item-basic-service` 2.1.0).

Plan data uses `A_FinPlanningEntryItem` (`API_FINPLANNINGENTRYITEM_SRV`), verified against
`@sap/cloud-sdk-vdm-financial-planning-entry-item-service` 2.1.0. Posting detail uses
`API_GLACCOUNTLINEITEM`; its fields are validated against the tenant's `$metadata` at first use.

Client behaviour: retry with backoff on 429/502/503/504 honouring `Retry-After`, `/Date()/` → ISO
dates, `$top/$skip` + `__next` paging restricted to the configured origin and service path with
repeated-link detection and a page cap, hard row ceiling, scope-partitioned master-data cache,
validation on every ID and field name, machine-readable error codes (`[QUERY_TOO_LARGE]`,
`[MIXED_CURRENCY]`, `[UNAUTHORIZED_SCOPE]`, `[PLAN_UNAVAILABLE]`, …), GET only.

Governance (per SAP ref-arch 137800 / API Policy v.4.2026a): backend data scope allowlists,
audit log per tool call, MCP rate limit, optional inbound API key, refusal of mixed-currency or
truncated totals. Compliance mapping and remaining tenant gates: `docs/CONNECTION_INPUTS.md`.

Why Python and not TypeScript: it matches CALM_MCP (same FastMCP stack, same GenAI Studio
deployment pattern), so the team operates one toolchain.

## Tenant setup (one-time, per tenant)

1. **Maintain Communication Users** → create a user (password or X.509 certificate).
2. **Maintain Communication Systems** → create a system, add the user as inbound user.
3. **Maintain Communication Arrangements** → create arrangements for:
   - **Required:** `API_JOURNALENTRYITEMBASIC_SRV` (SAP_COM_0087 or SAP_COM_0241 — confirm in tenant)
   - **Optional:** `API_COSTCENTER_SRV` (names), `API_FINPLANNINGENTRYITEM_SRV` (plan, SAP_COM_0087), `API_GLACCOUNTLINEITEM` (postings, SAP_COM_0303)
4. Note the API host: `https://<tenant>-api.s4hana.cloud.sap` (the `-api` host, not the Fiori host).
5. Run `s4hc_health(live=true)` → `ok`, or `ok_with_optional_services_missing` if only the required one is set up.

Full list of connection inputs (3 required, 5 optional): `docs/CONNECTION_INPUTS.md`.
GenAI Studio deployment: `docs/GENAI_STUDIO_DEPLOYMENT.md`.

Read-only API calls do not create documents, so SAP Digital Access document licensing is not triggered.
Confirm against the client's contract if in doubt.

## Run

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in S4HC_BASE_URL + credentials

python server.py                      # stdio
python server.py --http --port 8000   # http://127.0.0.1:8000/mcp
python -m pytest tests -q             # 57 tests: unit (fake OData backend) + HTTP end-to-end (header auth, scope, API key)
```

Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "sap-s4hana-cloud": {
      "command": "/path/to/S4HC_MCP/venv/bin/python",
      "args": ["/path/to/S4HC_MCP/server.py"],
      "env": { "S4HC_BASE_URL": "https://<tenant>-api.s4hana.cloud.sap", "S4HC_USER": "...", "S4HC_PASSWORD": "..." }
    }
  }
}
```

## GenAI Studio deployment

Same as CALM MCP: run `python server.py --http --host 0.0.0.0 --port 8000`, set
`S4HC_*` as deployment secrets (single-quote values containing `$`), keep
`MCP_TRUST_PROXY=true` behind the ingress. For several client tenants on one
deployment, leave the env vars empty and inject `x-s4hc-base-url`,
`x-s4hc-user`, `x-s4hc-password` per connection.

## Example prompts once connected

- "List the active cost centers in company code 1010 with their responsible person."
- "Give me a cost center summary for FY2026 periods 1–9 vs FY2025, ledger 0L."
- "What does cost center 4120 spend on? Break it down by G/L account." → `actuals_breakdown`
- "Which cost centers book travel (65000000)?" → `actuals_breakdown(dimension='CostCenter', gl_accounts=[...])`
- "Show the trailing 12 months for 4120 and flag anything unusual." → `cost_center_trend`
- "Export 2026 actuals for the IT cost centers as CSV." → `export_actuals_csv`

## Extending

- **Plan in SAC only:** `get_actual_vs_plan` reads plans stored in S/4HANA. If budgets live
  only in SAC, expose them via that system's read API or a Custom CDS view instead.
- **Governed access:** the same tools can be fronted by the SAP Integration Suite
  MCP Gateway when a client requires SAP-managed exposure.
- Writes are intentionally absent; the client only issues GET.
