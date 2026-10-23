"""Regression tests for the 2026-10 review and the live run against a real
Public Edition tenant. Each test fails on the code before that review."""

from __future__ import annotations

import os
import stat
from decimal import Decimal

import pytest

import fake_s4hc
from src.s4hc import config, dependencies
from src.s4hc.client import S4HCClient, S4HCError, _retry_after


class _Req:
    def __init__(self, headers):
        self.headers = {k.lower(): v for k, v in headers.items()}


class Ctx:
    """Stand-in for a FastMCP Context on an HTTP request with x-s4hc-* headers."""

    def __init__(self, headers):
        self.request_context = type("RC", (), {"request": _Req(headers)})()


def header_ctx(password="pw-one", user="comm_b", base="https://tenant-b-api.s4hana.cloud.sap", **extra):
    return Ctx({"x-s4hc-base-url": base, "x-s4hc-user": user, "x-s4hc-password": password, **extra})


# ------------------------------------------------------------ credentials
def test_header_host_never_gets_env_credentials():
    with pytest.raises(S4HCError, match="NOT_CONFIGURED"):
        dependencies.get_client(Ctx({"x-s4hc-base-url": "https://other-api.s4hana.cloud.sap"}))
    # naming the server's own host is fine: same tenant, same credentials
    c = dependencies.get_client(Ctx({"x-s4hc-base-url": os.environ["S4HC_BASE_URL"]}))
    assert c.base_url == os.environ["S4HC_BASE_URL"]


def test_user_header_requires_password_header():
    with pytest.raises(S4HCError, match="sent together"):
        dependencies.get_client(Ctx({"x-s4hc-user": "someone"}))
    with pytest.raises(S4HCError, match="sent together"):
        dependencies.get_client(Ctx({"x-s4hc-password": "x"}))


@pytest.mark.parametrize("url", ["https://internal.example.com", "http://tenant-api.s4hana.cloud.sap",
                                 "https://tenant-api.s4hana.cloud.sap/sap/opu", "https://u:p@tenant-api.s4hana.cloud.sap",
                                 "https://tenant-api.s4hana.cloud.sap.evil.com"])
def test_base_url_must_be_plain_sap_origin(url):
    with pytest.raises(S4HCError, match="NOT_CONFIGURED"):
        config.get_base_url(url)


def test_extra_host_suffix_can_be_allowed(monkeypatch):
    monkeypatch.setenv("S4HC_ALLOWED_HOST_SUFFIXES", ".s4hana.cloud.sap,.corp.example.com")
    assert config.get_base_url("https://sap.corp.example.com/") == "https://sap.corp.example.com"


def test_client_cache_keyed_by_password():
    a = dependencies.get_client(header_ctx("pw-one"))
    b = dependencies.get_client(header_ctx("pw-two"))
    assert a is not b and a.session.auth == ("comm_b", "pw-one") and b.session.auth == ("comm_b", "pw-two")
    assert dependencies.get_client(header_ctx("pw-one")) is a


def test_master_cache_not_shared_across_credentials(tool, calls):
    tool("list_company_codes")(ctx=header_ctx("pw-one"))
    n = len(calls)
    tool("list_company_codes")(ctx=header_ctx("pw-one"))
    assert len(calls) == n  # same credential: cached
    tool("list_company_codes")(ctx=header_ctx("wrong-password"))
    assert len(calls) == 2 * n  # other credential: asks SAP itself, never served the cached rows
    assert calls[-1]["auth"] == ("comm_b", "wrong-password")


def test_caches_are_bounded(monkeypatch):
    monkeypatch.setattr(dependencies, "MAX_CLIENTS", 3)
    for i in range(10):
        dependencies.get_client(header_ctx(f"pw-{i}"))
    assert len(dependencies._cached) == 3


def test_inline_pem_written_once_owner_only(monkeypatch):
    monkeypatch.setenv("S4HC_CLIENT_CERT_PEM", "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n")
    monkeypatch.setenv("S4HC_CLIENT_KEY_PEM", "-----BEGIN PRIVATE KEY-----\nBBBB\n-----END PRIVATE KEY-----\n")
    first = config.resolve_client_cert()
    assert config.resolve_client_cert() == first  # no new temp file per call
    assert stat.S_IMODE(os.stat(first[1]).st_mode) == 0o600


def test_redirect_is_not_followed(tool):
    fake_s4hc.STATE["redirect"] = True
    with pytest.raises(S4HCError, match="redirect"):
        tool("list_company_codes")(ctx=None)


def test_person_fields_are_opt_in(tool, monkeypatch):
    out = tool("list_cost_centers")(ctx=None)
    assert all("CostCtrResponsiblePersonName" not in c for c in out["cost_centers"])
    with pytest.raises(S4HCError, match="S4HC_EXPOSE_PERSON_FIELDS"):
        tool("list_cost_centers")(ctx=None, fields=["CostCenter", "CostCtrResponsiblePersonName"])
    monkeypatch.setenv("S4HC_EXPOSE_PERSON_FIELDS", "true")
    out = tool("list_cost_centers")(ctx=None, cost_center="CC100", fields=["CostCenter", "CostCtrResponsiblePersonName"])
    assert out["cost_centers"][0]["CostCtrResponsiblePersonName"] == "A. Person"


# ------------------------------------------------------------------ scope
def test_allowlist_header_cannot_widen_or_clear_env(tool, monkeypatch):
    monkeypatch.setenv("S4HC_ALLOWED_COMPANY_CODES", "1010")
    with pytest.raises(S4HCError, match="UNAUTHORIZED_SCOPE"):
        tool("list_company_codes")(ctx=Ctx({"x-s4hc-allowed-company-codes": ","}))
    with pytest.raises(S4HCError, match="UNAUTHORIZED_SCOPE"):
        tool("list_company_codes")(ctx=Ctx({"x-s4hc-allowed-company-codes": "1710"}))
    out = tool("list_company_codes")(ctx=Ctx({"x-s4hc-allowed-company-codes": "1010,1710"}))
    assert [c["CompanyCode"] for c in out["company_codes"]] == ["1010"]


def test_profit_centers_scope_fails_closed_without_company_column(tool, monkeypatch):
    monkeypatch.setenv("S4HC_ALLOWED_COMPANY_CODES", "1710")
    out = tool("list_profit_centers")(ctx=None, fields=["ProfitCenter", "Segment"])
    assert [p["ProfitCenter"] for p in out["profit_centers"]] == ["PC2"]
    assert out["profit_centers"][0]["company_codes"] == ["1710"]


def test_postings_apply_multi_company_allowlist_and_ledger(tool, calls, monkeypatch):
    monkeypatch.setenv("S4HC_ALLOWED_COMPANY_CODES", "1010,1710")
    out = tool("get_cost_center_postings")(ctx=None, cost_center="CC100", fiscal_year=2026, period_from=6, period_to=6)
    flt = [c for c in calls if c["path"].endswith("GLAccountLineItem")][0]["params"]["$filter"]
    assert "(CompanyCode eq '1010' or CompanyCode eq '1710')" in flt and "Ledger eq '0L'" in flt
    assert out["total"] == "4010.00" and out["count"] == 3  # the 2L copy of the travel line is not counted
    assert out["postings"][0]["FiscalPeriod"] == "006"


def test_large_cost_center_allowlist_filters_rows_instead_of_failing(tool, monkeypatch):
    monkeypatch.setenv("S4HC_ALLOWED_COST_CENTERS", ",".join(["CC300"] + [f"X{i:04d}" for i in range(250)]))
    out = tool("cost_center_summary")(ctx=None, fiscal_year=2026)
    assert [c["cost_center"] for c in out["cost_centers"]] == ["CC300"] and out["grand_total"] == "-40.00"


def test_filter_size_capped_across_lists(tool):
    with pytest.raises(S4HCError, match="QUERY_TOO_LARGE"):
        tool("get_cost_center_actuals")(ctx=None, fiscal_year=2026, cost_centers=[f"C{i}" for i in range(180)],
                                        gl_accounts=[f"{i:08d}" for i in range(180)])


def test_rate_limit_is_per_connection(monkeypatch):
    import asyncio

    from src.s4hc.governance import AuditAndRateLimitMiddleware
    monkeypatch.setenv("S4HC_RATE_LIMIT_PER_MIN", "1")
    mw = AuditAndRateLimitMiddleware()

    class Msg:
        name, arguments = "fiscal_context", {}

    def call(ctx):
        class MC:
            message = Msg()
            fastmcp_context = ctx

        async def ok(_):
            return "ok"
        return asyncio.run(mw.on_call_tool(MC(), ok))

    assert call(header_ctx("pw-a")) == "ok"
    with pytest.raises(S4HCError, match="Rate limit"):
        call(header_ctx("pw-a"))
    assert call(header_ctx("pw-b")) == "ok"  # another tenant/credential is not locked out


# --------------------------------------------------- financial correctness
def test_cost_center_tools_ignore_lines_without_cost_center(tool, calls):
    out = tool("cost_center_summary")(ctx=None, fiscal_year=2026, company_code="1010")
    assert "CostCenter ne ''" in calls[-1]["params"]["$filter"]
    assert "" not in {c["cost_center"] for c in out["cost_centers"]}
    assert out["grand_total"] == "15330.50"  # 12x1010 + 250.50 + 3000 - 40; the payables lines would net it to 0
    everything = tool("get_cost_center_actuals")(ctx=None, fiscal_year=2026, group_by=["CompanyCode"],
                                                 cost_center_lines_only=False)
    assert everything["total"] == "0.00"


def test_periods_are_padded_and_complete_even_when_sap_strips_zeros(tool):
    out = tool("cost_center_summary")(ctx=None, fiscal_year=2026, period_from=1, period_to=12)
    assert [p["period"] for p in out["by_period"]] == [f"{i:03d}" for i in range(1, 13)]
    rows = tool("get_cost_center_actuals")(ctx=None, fiscal_year=2026, cost_centers=["CC300"])["rows"]
    assert rows[0]["FiscalPeriod"] == "001"


def test_trend_fills_gaps_and_flags_only_real_spikes(tool):
    out = tool("cost_center_trend")(ctx=None, cost_centers=["CC300"], fiscal_year_from=2025, period_from=11,
                                    fiscal_year_to=2026, period_to=3)
    s = out["cost_centers"][0]["series"]
    assert [p["period"] for p in s] == ["2025/011", "2025/012", "2026/001", "2026/002", "2026/003"]
    assert [p["amount"] for p in s] == ["0", "0", "-40.00", "0", "0"]
    assert s[3]["mom_change"] == "40.00"  # adjacent periods, not the next period that happened to have postings
    cc100 = tool("cost_center_trend")(ctx=None, cost_centers=["CC100"], fiscal_year_from=2026, fiscal_year_to=2026)
    assert cc100["cost_centers"][0]["anomalous_periods"] == ["2026/006"]


def test_breakdown_by_every_named_dimension(tool):
    out = tool("actuals_breakdown")(ctx=None, dimension="Segment", fiscal_year=2026, company_code="1010")
    assert {i["Segment"]: i["name"] for i in out["items"]} == {"S1": "Segment S1", "S2": "Segment S2"}
    for dim in ("BusinessArea", "Plant", "Project", "WBSElement", "CostCtrActivityType", "PartnerCostCenter"):
        tool("actuals_breakdown")(ctx=None, dimension=dim, fiscal_year=2026)


def test_year_over_year_refuses_currency_change(tool, monkeypatch):
    usd_2024 = {**fake_s4hc.LINE_ITEMS[0], "LedgerFiscalYear": "2024", "CompanyCodeCurrency": "USD"}
    monkeypatch.setattr(fake_s4hc, "LINE_ITEMS", fake_s4hc.LINE_ITEMS + [usd_2024])
    with pytest.raises(S4HCError, match="MIXED_CURRENCY"):
        tool("cost_center_summary")(ctx=None, fiscal_year=2026, compare_fiscal_year=2024)
    with pytest.raises(S4HCError, match="MIXED_CURRENCY"):
        tool("actuals_breakdown")(ctx=None, fiscal_year=2026, compare_fiscal_year=2024)


def test_summary_keeps_cost_centers_that_dropped_to_zero(tool):
    out = tool("cost_center_summary")(ctx=None, fiscal_year=2025, compare_fiscal_year=2026)
    assert "CC300" in {c["cost_center"] for c in out["cost_centers"]}


def test_csv_neutralises_formulas_but_not_negative_amounts(tool, monkeypatch):
    evil = {**fake_s4hc.LINE_ITEMS[0], "CostCenter": "CC300", "CostCenterName": "=HYPERLINK(\"x\")",
            "AmountInCompanyCodeCurrency": "-5"}
    monkeypatch.setattr(fake_s4hc, "LINE_ITEMS", [evil])
    out = tool("export_actuals_csv")(ctx=None, fiscal_year=2026, group_by=["CostCenter", "CostCenterName"])
    assert out["csv"].splitlines()[1] == 'CC300,"\'=HYPERLINK(""x"")",-5,CAD'


def test_fiscal_context_warns_on_unknown_company_code(tool):
    out = tool("fiscal_context")(ctx=None, company_code="9999")
    assert out["company_code_fiscal_year_variant"] is None and "does not exist" in out["warnings"][0]


# ------------------------------------------------------------ master data
def test_profit_centers_use_assignment_and_names(tool):
    out = tool("list_profit_centers")(ctx=None, company_code="1710")
    assert [(p["ProfitCenter"], p["ProfitCenterName"]) for p in out["profit_centers"]] == [("PC2", "Online EN")]
    assert out["profit_centers"][0]["company_codes"] == ["1010", "1710"]
    assert out["company_code_source"].startswith("API_PROFITCENTER_SRV")


def test_profit_centers_fallback_derives_company_codes_from_cost_centers(tool):
    fake_s4hc.STATE["forbid_pc_srv"] = True
    out = tool("list_profit_centers")(ctx=None, company_code="1010")
    assert [p["ProfitCenter"] for p in out["profit_centers"]] == ["PC1", "PC2"]
    assert "ProfitCenterName" not in out["profit_centers"][0] and out["completeness"]["warnings"]


def test_gl_accounts_carry_names_or_say_why_not(tool):
    out = tool("list_gl_accounts")(ctx=None, chart_of_accounts="YCOA", only_pl=True)
    assert {g["GLAccount"]: g["GLAccountName"] for g in out["gl_accounts"]} == {"61000000": "Salaries EN",
                                                                                "65000000": "Travel EN"}
    fake_s4hc.STATE["forbid_glacct_srv"] = True
    dependencies._master_cache.clear()
    out = tool("list_gl_accounts")(ctx=None, chart_of_accounts="YCOA", only_pl=True)
    assert "names unavailable" in out["completeness"]["warnings"][0]


def test_list_tools_carry_envelope(tool):
    for name, kw in (("list_company_codes", {}), ("list_cost_centers", {}), ("list_profit_centers", {}),
                     ("list_gl_accounts", {"chart_of_accounts": "YCOA"})):
        out = tool(name)(ctx=None, **kw)
        assert out["completeness"]["status"] in ("complete", "partial") and out["provenance"]["data_notice"]


def test_list_cost_centers_multi_company_or_filter(tool, calls, monkeypatch):
    monkeypatch.setenv("S4HC_ALLOWED_COMPANY_CODES", "1010,1710")
    tool("list_cost_centers")(ctx=None)
    assert "(CompanyCode eq '1010' or CompanyCode eq '1710')" in calls[0]["params"]["$filter"]


def test_company_codes_use_default_controlling_area(tool, calls, monkeypatch):
    monkeypatch.setenv("S4HC_CONTROLLING_AREA", "A000")
    tool("list_company_codes")(ctx=None)
    assert "ControllingArea eq 'A000'" in calls[0]["params"]["$filter"]


def test_describe_service_cached(tool, calls):
    tool("describe_service")(ctx=None, service="profitcenter")
    n = len(calls)
    tool("describe_service")(ctx=None, service="profitcenter")
    assert len(calls) == n


# ------------------------------------------------------------------ health
def test_health_detects_metadata_without_data_access(tool):
    fake_s4hc.STATE["gl_data_forbidden"] = True
    out = tool("s4hc_health")(ctx=None, live=True)
    gl = out["services"]["API_GLACCOUNTLINEITEM"]
    assert out["status"] == "ok_with_optional_services_missing" and gl["ok"] is False
    assert gl["entity_sets"] == ["GLAccountLineItem"] and "arrangement" in gl["hint"]


def test_health_error_when_required_service_down(tool):
    fake_s4hc.STATE["je_down"] = True
    out = tool("s4hc_health")(ctx=None, live=True)
    assert out["status"] == "error" and not out["services"]["API_JOURNALENTRYITEMBASIC_SRV"]["ok"]


def test_health_reports_bad_configuration(tool, monkeypatch):
    monkeypatch.setenv("S4HC_BASE_URL", "https://not-sap.example.com")
    out = tool("s4hc_health")(ctx=None, live=True)
    assert out["status"] == "error" and "not an allowed" in out["error"]


# ------------------------------------------------------------------ client
def test_client_without_credentials_is_not_configured():
    with pytest.raises(S4HCError, match="NOT_CONFIGURED"):
        S4HCClient("https://x-api.s4hana.cloud.sap")


def test_server_driven_paging_follows_next_on_short_pages(tool, calls):
    fake_s4hc.STATE["server_page"] = 1  # SAP returns 1 row per page even though $top=2, plus __next
    out = tool("list_company_codes")(ctx=None)
    assert out["count"] == 2 and out["truncated"] is False
    assert "$skiptoken" in calls[-1]["path"] + str(calls[-1]["params"])


def test_max_pages_refused(tool, monkeypatch):
    monkeypatch.setenv("S4HC_MAX_PAGES", "1")
    with pytest.raises(S4HCError, match="QUERY_TOO_LARGE"):
        tool("list_cost_centers")(ctx=None)


def test_truncation_is_exact_at_the_ceiling(tool, monkeypatch):
    monkeypatch.setenv("S4HC_MAX_ROWS", "2")
    out = tool("list_company_codes")(ctx=None)  # exactly 2 company codes: not truncated
    assert out["count"] == 2 and out["truncated"] is False


def test_sap_row_metadata_is_stripped(tool):
    out = tool("list_cost_centers")(ctx=None)
    assert all("__metadata" not in c and "to_Navigation" not in c for c in out["cost_centers"])


@pytest.mark.parametrize("value,expected", [("2", 2.0), ("-5", 0.0), ("nan", 0.5), ("garbage", 0.5),
                                            ("Wed, 21 Oct 2015 07:28:00 GMT", 0.0), ("100000", 30.0), (None, 0.5)])
def test_retry_after_never_crashes(value, expected):
    assert _retry_after(value, 0) == expected


def test_retry_after_http_date_is_retried(tool, calls):
    fake_s4hc.STATE["throttle_once"] = True
    original = fake_s4hc.fake_get

    def throttled(self, url, params=None, headers=None, timeout=None, **kw):
        r = original(self, url, params, headers, timeout, **kw)
        if r.status_code == 429:
            r.headers = {"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}
        return r

    import requests
    requests.Session.get = throttled
    try:
        assert tool("list_company_codes")(ctx=None)["count"] == 2
    finally:
        requests.Session.get = original


def test_amounts_stay_decimal(tool):
    out = tool("cost_center_summary")(ctx=None, fiscal_year=2026, cost_centers=["CC100"])
    assert Decimal(out["grand_total"]) == Decimal("15370.50")
