"""Unit tests for every tool against the fake OData backend (no network)."""

from __future__ import annotations

import pytest

import fake_s4hc
from src.s4hc.client import S4HCError, normalize_value


# ----------------------------------------------------------------- client
def test_date_normalization():
    assert normalize_value("/Date(253402214400000)/") == "9999-12-31"
    assert normalize_value("/Date(1577836800000)/") == "2020-01-01"
    assert normalize_value("plain") == "plain"
    assert normalize_value(5) == 5


def test_retry_on_429(tool, calls):
    fake_s4hc.STATE["throttle_once"] = True
    out = tool("list_company_codes")(ctx=None)
    assert out["count"] == 2
    assert calls[0]["path"] == calls[1]["path"]  # first 429, then retried with same request


def test_401_is_clear(tool):
    fake_s4hc.STATE["bad_auth"] = True
    with pytest.raises(S4HCError, match="401"):
        tool("list_company_codes")(ctx=None)


# ----------------------------------------------------------------- health
def test_health_live_ok(tool):
    out = tool("s4hc_health")(ctx=None, live=True)
    assert out["status"] == "ok" and out["auth_mode"] == "basic"
    assert out["services"]["API_JOURNALENTRYITEMBASIC_SRV"]["required"] is True
    assert "A_CostCenter" in out["services"]["API_COSTCENTER_SRV"]["entity_sets"]


def test_health_without_costcenter_srv(tool):
    fake_s4hc.STATE["forbid_costcenter_srv"] = True
    out = tool("s4hc_health")(ctx=None, live=True)
    assert out["status"] == "ok_with_optional_services_missing"


def test_health_unconfigured(tool, monkeypatch):
    monkeypatch.delenv("S4HC_USER")
    out = tool("s4hc_health")(ctx=None)
    assert out["status"] == "unconfigured" and "password" not in str(out).lower()


# ------------------------------------------------------------ master data
def test_list_company_codes_cached(tool, calls):
    a = tool("list_company_codes")(ctx=None)
    b = tool("list_company_codes")(ctx=None)
    assert a["company_codes"][0]["ChartOfAccounts"] == "YCOA" and a is b
    n = len(calls)
    tool("list_company_codes")(ctx=None)
    assert len(calls) == n  # served from cache


def test_list_profit_centers(tool):
    out = tool("list_profit_centers")(ctx=None, controlling_area="A000", segment="S2")
    assert [p["ProfitCenter"] for p in out["profit_centers"]] == ["PC2"]
    assert out["profit_centers"][0]["ValidityEndDate"] == "9999-12-31"


def test_list_gl_accounts_filters(tool, calls):
    out = tool("list_gl_accounts")(ctx=None, chart_of_accounts="YCOA", only_pl=True, gl_account_from="60000000", gl_account_to="69999999")
    assert [g["GLAccount"] for g in out["gl_accounts"]] == ["61000000", "65000000"]
    assert "IsProfitLossAccount eq true" in calls[0]["params"]["$filter"]


# ----------------------------------------------------------- cost centers
def test_list_cost_centers_pages_picks_language_and_current(tool, calls):
    out = tool("list_cost_centers")(ctx=None, controlling_area="A000")
    assert out["count"] == 3 and out["source"] == "API_COSTCENTER_SRV"
    assert [c["CostCenterName"] for c in out["cost_centers"]] == ["Sales", None, "IT"]
    assert out["cost_centers"][0]["ValidityStartDate"] == "2020-01-01"
    assert len([c for c in calls if c["path"].endswith("A_CostCenter")]) == 2  # page size 2
    assert "$expand" in calls[0]["params"] and calls[0]["params"]["$expand"] == "to_Text"


def test_list_cost_centers_fallback(tool):
    fake_s4hc.STATE["forbid_costcenter_srv"] = True
    out = tool("list_cost_centers")(ctx=None, company_code="1010", hierarchy_area="H2")
    assert out["source"].startswith("API_JOURNALENTRYITEMBASIC_SRV")
    assert [c["CostCenter"] for c in out["cost_centers"]] == ["CC300"]


def test_list_cost_centers_rejects_bad_input(tool):
    with pytest.raises(S4HCError):
        tool("list_cost_centers")(ctx=None, cost_center="x' or 1 eq 1")
    with pytest.raises(S4HCError):
        tool("list_cost_centers")(ctx=None, fields=["NotAField"])


# ---------------------------------------------------------------- actuals
def test_actuals_aggregates_server_side(tool, calls):
    out = tool("get_cost_center_actuals")(ctx=None, fiscal_year=2026, cost_centers=["CC100"], period_from=1, period_to=3)
    # periods 1-3 salaries 1010 each (two lines aggregated) + travel 250.50 in p2
    assert out["count"] == 4 and out["total"] == "3280.50"
    q = calls[0]["params"]
    assert "Ledger eq '0L'" in q["$filter"] and "LedgerFiscalYear eq '2026'" in q["$filter"]
    assert "FiscalPeriod ge '001' and FiscalPeriod le '003'" in q["$filter"]
    assert q["$select"] == "CostCenter,CostCenterName,GLAccount,GLAccountName,FiscalPeriod,CompanyCodeCurrency,AmountInCompanyCodeCurrency"
    assert all(r["Currency"] == "CAD" for r in out["rows"])


def test_actuals_defaults_year_and_global_currency(tool, calls):
    from datetime import date
    out = tool("get_cost_center_actuals")(ctx=None, currency="global", group_by=["CostCenter"])
    assert out["filter"]["fiscal_year"] == date.today().year
    assert "AmountInGlobalCurrency" in calls[0]["params"]["$select"]


def test_actuals_multi_filters(tool, calls):
    tool("get_cost_center_actuals")(ctx=None, fiscal_year=2026, profit_centers=["PC2"], functional_areas=["YB20"], gl_accounts=["62000000"])
    f = calls[0]["params"]["$filter"]
    assert "ProfitCenter eq 'PC2'" in f and "FunctionalArea eq 'YB20'" in f and "GLAccount eq '62000000'" in f


def test_actuals_bad_period(tool):
    with pytest.raises(S4HCError):
        tool("get_cost_center_actuals")(ctx=None, fiscal_year=2026, period_from=5, period_to=2)


def test_summary_with_comparison(tool):
    out = tool("cost_center_summary")(ctx=None, fiscal_year=2026, compare_fiscal_year=2025, period_from=1, period_to=1)
    cc = {c["cost_center"]: c for c in out["cost_centers"]}
    assert cc["CC100"]["total"] == "1010" and cc["CC100"]["compare_total"] == "900"
    assert cc["CC100"]["variance"] == "110" and cc["CC100"]["variance_pct"] == "12.2"
    assert cc["CC300"]["variance_pct"] is None
    assert out["grand_total"] == "970.00" and out["grand_variance_pct"] == "7.8"
    assert out["top_gl_accounts"][0]["gl_account"] == "61000000"


# --------------------------------------------------------------- analysis
def test_fiscal_context(tool):
    out = tool("fiscal_context")(ctx=None)
    assert out["default_ledger"] == "0L" and out["ytd_periods"][0] == 1


def test_breakdown_by_gl_for_one_cost_center(tool):
    out = tool("actuals_breakdown")(ctx=None, dimension="GLAccount", fiscal_year=2026, cost_centers=["CC100"])
    items = {i["GLAccount"]: i for i in out["items"]}
    assert items["61000000"]["total"] == "12120" and items["65000000"]["total"] == "3250.50"
    assert items["61000000"]["name"] == "Salaries"
    assert float(items["61000000"]["share_pct"]) + float(items["65000000"]["share_pct"]) == pytest.approx(100, abs=0.2)


def test_breakdown_rejects_bad_dimension(tool):
    with pytest.raises(S4HCError):
        tool("actuals_breakdown")(ctx=None, dimension="AmountInCompanyCodeCurrency")


def test_trend_cross_year_and_anomaly(tool, calls):
    out = tool("cost_center_trend")(ctx=None, cost_centers=["CC100"], fiscal_year_from=2025, period_from=12, fiscal_year_to=2026, period_to=12)
    years = {c["params"]["$filter"].split("LedgerFiscalYear eq '")[1][:4] for c in calls if c["path"].endswith("A_JournalEntryItemBasic")}
    assert years == {"2025", "2026"}  # one query per fiscal year
    cc = out["cost_centers"][0]
    assert cc["periods"] == 13 and cc["series"][0]["period"] == "2025/012"
    assert "2026/006" in cc["anomalous_periods"]  # June travel spike
    assert cc["series"][1]["mom_change"] == "60"  # 950 -> 1010


def test_trend_limits(tool):
    with pytest.raises(S4HCError):
        tool("cost_center_trend")(ctx=None, fiscal_year_from=2020, fiscal_year_to=2026)


def test_export_csv(tool):
    out = tool("export_actuals_csv")(ctx=None, fiscal_year=2026, cost_centers=["CC300"])
    lines = out["csv"].strip().splitlines()
    assert lines[0] == "CostCenter,CostCenterName,GLAccount,GLAccountName,FiscalPeriod,Amount,Currency"
    assert lines[1] == "CC300,IT,62000000,Software,001,-40.00,CAD"
    assert out["filename"] == "cost_center_actuals_2026_001-012.csv"


# ----------------------------------------------------------- odata_read
def test_describe_service(tool):
    d = tool("describe_service")(ctx=None, service="costcenter")
    assert d["entity_sets"]["A_CostCenter"] == "A_CostCenterType"
    assert d["entity_types"]["A_CostCenterType"]["navigation"] == ["to_Text"]


def test_generic_read_disabled_by_default(tool):
    with pytest.raises(S4HCError, match="DISABLED"):
        tool("s4hc_odata_read")(ctx=None, service="API_COSTCENTER_SRV", entity_set="A_CostCenter")


def test_generic_read_refused_under_scope(tool, monkeypatch):
    monkeypatch.setenv("S4HC_ENABLE_RAW_ODATA", "true")
    monkeypatch.setenv("S4HC_ALLOWED_COST_CENTERS", "CC100")
    with pytest.raises(S4HCError, match="UNAUTHORIZED_SCOPE"):
        tool("s4hc_odata_read")(ctx=None, service="API_COSTCENTER_SRV", entity_set="A_CostCenter")


def test_generic_read_guards(tool, monkeypatch):
    monkeypatch.setenv("S4HC_ENABLE_RAW_ODATA", "true")
    with pytest.raises(S4HCError):
        tool("s4hc_odata_read")(ctx=None, service="API_COSTCENTER_SRV", entity_set="A_CostCenter", params={"$top": "5"})
    with pytest.raises(S4HCError):
        tool("s4hc_odata_read")(ctx=None, service="../etc", entity_set="A_CostCenter")
    with pytest.raises(S4HCError, match="403"):
        tool("s4hc_odata_read")(ctx=None, service="API_FORBIDDEN_SRV", entity_set="Forbidden")
    ok = tool("s4hc_odata_read")(ctx=None, service="API_JOURNALENTRYITEMBASIC_SRV", entity_set="A_CompanyCode", params={"$filter": "CompanyCode eq '1710'"})
    assert ok["count"] == 1 and ok["rows"][0]["Currency"] == "USD"


# ------------------------------------------------------------- governance
def test_scope_env_narrows_and_blocks(tool, calls, monkeypatch):
    monkeypatch.setenv("S4HC_ALLOWED_COST_CENTERS", "CC100,CC200")
    monkeypatch.setenv("S4HC_ALLOWED_COMPANY_CODES", "1010")
    out = tool("get_cost_center_actuals")(ctx=None, fiscal_year=2026, group_by=["CostCenter"])
    f = calls[0]["params"]["$filter"]
    assert "CostCenter eq 'CC100' or CostCenter eq 'CC200'" in f and "CompanyCode eq '1010'" in f
    assert {r["CostCenter"] for r in out["rows"]} == {"CC100"}
    with pytest.raises(S4HCError, match="outside"):
        tool("get_cost_center_actuals")(ctx=None, fiscal_year=2026, cost_centers=["CC300"])
    with pytest.raises(S4HCError, match="outside"):
        tool("cost_center_summary")(ctx=None, fiscal_year=2026, company_code="1710")
    ccs = tool("list_cost_centers")(ctx=None)
    assert {c["CostCenter"] for c in ccs["cost_centers"]} == {"CC100", "CC200"}
    cos = tool("list_company_codes")(ctx=None)
    assert [c["CompanyCode"] for c in cos["company_codes"]] == ["1010"]


def test_scope_multi_company_codes_or_filter(tool, calls, monkeypatch):
    monkeypatch.setenv("S4HC_ALLOWED_COMPANY_CODES", "1010,1710")
    tool("get_cost_center_actuals")(ctx=None, fiscal_year=2026, group_by=["CostCenter"])
    assert "(CompanyCode eq '1010' or CompanyCode eq '1710')" in calls[0]["params"]["$filter"]


def test_rate_limit_middleware(monkeypatch):
    import asyncio
    from src.s4hc.governance import AuditAndRateLimitMiddleware
    monkeypatch.setenv("S4HC_RATE_LIMIT_PER_MIN", "2")
    mw = AuditAndRateLimitMiddleware()

    class Msg:  # minimal stand-in for CallToolRequestParams
        name, arguments = "fiscal_context", {"fiscal_year": 2026, "ignored": "x"}

    class Ctx:
        message = Msg(); fastmcp_context = None

    async def ok(_):
        return "ok"

    assert asyncio.run(mw.on_call_tool(Ctx(), ok)) == "ok"
    assert asyncio.run(mw.on_call_tool(Ctx(), ok)) == "ok"
    with pytest.raises(S4HCError, match="Rate limit"):
        asyncio.run(mw.on_call_tool(Ctx(), ok))


def test_audit_log_has_no_secrets(caplog):
    """Goes through the real MCP call path (middleware included) and checks every
    log line, not just audit ones, for the password as a substring."""
    import asyncio
    import logging

    from fastmcp import Client

    import server
    from conftest import TEST_PASSWORD

    caplog.set_level(logging.DEBUG)

    async def run():
        async with Client(server.mcp) as c:
            await c.call_tool("list_company_codes", {"company_code": "1010"})
            await c.call_tool("s4hc_health", {"live": True})

    asyncio.run(run())
    audit = [r.getMessage() for r in caplog.records if r.name == "s4hc-mcp.audit"]
    assert any("tool=list_company_codes" in m and "outcome=ok" in m for m in audit)  # the middleware really ran
    assert all(TEST_PASSWORD not in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------- compare
def test_compare_periods_with_drivers(tool):
    out = tool("compare_cost_center_periods")(ctx=None, base_fiscal_year=2026, base_period_from=6, base_period_to=6,
                                               compare_fiscal_year=2026, compare_period_from=5, compare_period_to=5, cost_centers=["CC100"])
    cc = out["cost_centers"][0]
    assert cc["base_total"] == "4010.00" and cc["compare_total"] == "1010" and cc["variance"] == "3000.00"
    assert cc["drivers"][0]["gl_account"] == "65000000" and cc["drivers"][0]["variance"] == "3000.00"
    assert out["variance_pct"] == "297.0"


# --------------------------------------------------------------- postings
def test_postings_discovers_fields_and_bounds(tool, calls):
    out = tool("get_cost_center_postings")(ctx=None, cost_center="CC100", fiscal_year=2026, period_from=6, period_to=6, min_abs_amount=500)
    assert out["source"].endswith("API_GLACCOUNTLINEITEM/GLAccountLineItem")
    assert [p["AccountingDocument"] for p in out["postings"]] == ["1900000001", "1900000003"]  # taxi (200) filtered out
    assert out["postings"][0]["ItemText"] == "Conference Berlin" and out["postings"][0]["PostingDate"] == "2026-06-16"
    assert out["total"] == "3810.00"
    q = [c for c in calls if c["path"].endswith("GLAccountLineItem")][0]["params"]
    assert "CostCenter eq 'CC100'" in q["$filter"] and "FiscalYear eq '2026'" in q["$filter"] and q["$orderby"] == "PostingDate desc"


def test_postings_clear_error_without_arrangement(tool):
    fake_s4hc.STATE["forbid_gl_srv"] = True
    with pytest.raises(S4HCError, match="403"):
        tool("get_cost_center_postings")(ctx=None, cost_center="CC100", fiscal_year=2026)


def test_postings_respects_scope(tool, monkeypatch):
    monkeypatch.setenv("S4HC_ALLOWED_COST_CENTERS", "CC300")
    with pytest.raises(S4HCError, match="outside"):
        tool("get_cost_center_postings")(ctx=None, cost_center="CC100", fiscal_year=2026)


# ------------------------------------------------- financial correctness
def test_mixed_currency_refused_for_totals(tool):
    fake_s4hc.STATE["usd_line"] = True
    with pytest.raises(S4HCError, match="MIXED_CURRENCY"):
        tool("cost_center_summary")(ctx=None, fiscal_year=2026)
    with pytest.raises(S4HCError, match="MIXED_CURRENCY"):
        tool("actuals_breakdown")(ctx=None, fiscal_year=2026)
    # filtering to one company code resolves it
    out = tool("cost_center_summary")(ctx=None, fiscal_year=2026, company_code="1010")
    assert out["currency"] == ["CAD"]
    # global currency also resolves it
    tool("cost_center_summary")(ctx=None, fiscal_year=2026, currency="global")


def test_listing_tool_reports_per_currency_not_single_total(tool):
    fake_s4hc.STATE["usd_line"] = True
    out = tool("get_cost_center_actuals")(ctx=None, fiscal_year=2026, group_by=["CompanyCode"])
    assert out["total"] is None and set(out["totals_by_currency"]) == {"CAD", "USD"}
    assert out["completeness"]["warnings"]


def test_truncated_aggregate_refused(tool, monkeypatch):
    monkeypatch.setenv("S4HC_MAX_ROWS", "3")
    with pytest.raises(S4HCError, match="QUERY_TOO_LARGE"):
        tool("cost_center_summary")(ctx=None, fiscal_year=2026)
    out = tool("get_cost_center_actuals")(ctx=None, fiscal_year=2026)
    assert out["truncated"] is True and out["completeness"]["status"] == "partial"


def test_envelope_present(tool):
    out = tool("cost_center_summary")(ctx=None, fiscal_year=2026)
    assert out["completeness"]["status"] == "complete"
    assert out["provenance"]["source_api"].endswith("A_JournalEntryItemBasic") and out["provenance"]["retrieved_at"]


def test_special_and_carryforward_periods_allowed(tool, calls):
    tool("get_cost_center_actuals")(ctx=None, fiscal_year=2026, period_from=0, period_to=16)
    assert "FiscalPeriod ge '000' and FiscalPeriod le '016'" in calls[0]["params"]["$filter"]


def test_offsite_next_link_refused(tool):
    fake_s4hc.STATE["next_link"] = "https://evil.example.com/steal"
    with pytest.raises(S4HCError, match="INCOMPLETE_RESULT"):
        tool("list_company_codes")(ctx=None)


def test_repeated_next_link_refused(tool):
    fake_s4hc.STATE["next_link"] = "https://my-api.s4hana.cloud.sap/sap/opu/odata/sap/API_JOURNALENTRYITEMBASIC_SRV/A_CompanyCode?$skiptoken=1"
    with pytest.raises(S4HCError, match="repeated"):
        tool("list_company_codes")(ctx=None)


def test_error_codes_are_machine_readable(tool):
    with pytest.raises(S4HCError) as e:
        tool("cost_center_trend")(ctx=None, fiscal_year_from=2020, fiscal_year_to=2026)
    assert e.value.code == "QUERY_TOO_LARGE" and not e.value.retryable


def test_fiscal_context_reads_fyv(tool):
    out = tool("fiscal_context")(ctx=None, company_code="1010")
    assert out["company_code_fiscal_year_variant"] == "K4" and out["warnings"] == []


# ---------------------------------------------------------------- plan
def test_plan_lists_categories_first(tool):
    out = tool("get_actual_vs_plan")(ctx=None, fiscal_year=2026)
    cats = {c["planning_category"] for c in out["planning_categories"]}
    assert cats == {"PLN", "FCST"}


def test_actual_vs_plan_by_cost_center(tool, calls):
    out = tool("get_actual_vs_plan")(ctx=None, planning_category="PLN", fiscal_year=2026, period_from=1, period_to=6)
    items = {i["CostCenter"]: i for i in out["items"]}
    # CC100 actual p1-6: 6*1010 + 250.50 + 3000 = 9310.50 ; plan 6*1100 = 6600
    assert items["CC100"]["actual"] == "9310.50" and items["CC100"]["plan"] == "6600"
    assert items["CC100"]["variance"] == "2710.50" and items["CC100"]["actual_above_plan"] is True
    assert items["CC100"]["consumption_pct"] == "141.1"
    # CC300 actual -40 (p1) vs plan 300
    assert items["CC300"]["actual"] == "-40.00" and items["CC300"]["actual_above_plan"] is False
    plan_q = [c for c in calls if c["path"].endswith("A_FinPlanningEntryItem")][0]["params"]["$filter"]
    assert "PlanningCategory eq 'PLN'" in plan_q and "Ledger eq '0L'" in plan_q


def test_actual_vs_plan_by_gl(tool):
    out = tool("get_actual_vs_plan")(ctx=None, planning_category="PLN", fiscal_year=2026, dimension="GLAccount", cost_centers=["CC100"])
    items = {i["GLAccount"]: i for i in out["items"]}
    assert items["65000000"]["plan"] == "1200" and items["65000000"]["actual"] == "3250.50"


def test_plan_unavailable(tool):
    fake_s4hc.STATE["forbid_plan_srv"] = True
    with pytest.raises(S4HCError, match="PLAN_UNAVAILABLE"):
        tool("get_actual_vs_plan")(ctx=None, planning_category="PLN", fiscal_year=2026)
    fake_s4hc.STATE["forbid_plan_srv"] = False
    with pytest.raises(S4HCError, match="PLAN_UNAVAILABLE"):
        tool("get_actual_vs_plan")(ctx=None, planning_category="NOPE", fiscal_year=2026)


def test_plan_respects_scope(tool, monkeypatch):
    monkeypatch.setenv("S4HC_ALLOWED_COST_CENTERS", "CC300")
    out = tool("get_actual_vs_plan")(ctx=None, planning_category="PLN", fiscal_year=2026)
    assert {i["CostCenter"] for i in out["items"]} == {"CC300"}


def test_cache_partitioned_by_scope(tool, monkeypatch):
    full = tool("list_company_codes")(ctx=None)
    assert full["count"] == 2
    monkeypatch.setenv("S4HC_ALLOWED_COMPANY_CODES", "1010")
    scoped = tool("list_company_codes")(ctx=None)
    assert [c["CompanyCode"] for c in scoped["company_codes"]] == ["1010"]
    monkeypatch.delenv("S4HC_ALLOWED_COMPANY_CODES")
    assert tool("list_company_codes")(ctx=None)["count"] == 2
