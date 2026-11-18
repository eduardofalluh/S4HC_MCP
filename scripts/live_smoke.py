"""Live smoke test: run all 16 tools against a real S/4HANA Cloud tenant.

Uses the same S4HC_* settings as the server (environment or .env). Discovers a
company code, chart of accounts and cost center itself, so nothing about the
tenant is hard-coded. Prints one line per check — counts and totals only,
never credentials or row data — and exits non-zero if any check fails.

    python scripts/live_smoke.py
    S4HC_SMOKE_COMPANY_CODE=1710 S4HC_SMOKE_FISCAL_YEAR=2026 python scripts/live_smoke.py

Optional services the communication user cannot read (postings, plan) are
reported as SKIP, not FAIL; s4hc_health(live=true) says which arrangement is missing.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402  (loads .env and registers the tools)
from src.s4hc.errors import S4HCError  # noqa: E402

OPTIONAL_CODES = {"SAP_FORBIDDEN", "SAP_NOT_FOUND", "PLAN_UNAVAILABLE"}
results: list[tuple[str, str, str]] = []


def tool(name):
    return asyncio.run(server.mcp.get_tool(name)).fn


def check(label: str, name: str, summarize, expect_code: str | None = None, **kwargs):
    t0 = time.monotonic()
    try:
        out = tool(name)(ctx=None, **kwargs)
    except S4HCError as exc:
        if expect_code and exc.code == expect_code:
            results.append(("PASS", label, f"refused as expected ({exc.code})"))
        elif exc.code in OPTIONAL_CODES and not expect_code:
            results.append(("SKIP", label, f"{exc.code}: optional service not available to this user"))
        else:
            results.append(("FAIL", label, str(exc)[:200]))
        return None
    except Exception as exc:  # noqa: BLE001
        results.append(("FAIL", label, f"{exc.__class__.__name__}: {str(exc)[:200]}"))
        return None
    if expect_code:
        results.append(("FAIL", label, f"expected {expect_code}, got a result"))
        return out
    try:
        note = summarize(out)
        results.append(("PASS", label, f"{note} ({time.monotonic() - t0:.1f}s)"))
    except AssertionError as exc:
        results.append(("FAIL", label, f"check failed: {exc}"))
    return out


def main() -> int:
    fy = int(os.getenv("S4HC_SMOKE_FISCAL_YEAR") or date.today().year)

    health = check("health (live)", "s4hc_health", lambda r: f"status={r['status']}", live=True)
    if not health or health.get("status") == "error":
        print("Required service unreachable; fix the connection first.")
        _print()
        return 1
    check("fiscal context", "fiscal_context", lambda r: f"FY{r['fiscal_year']} P{r['fiscal_period']}")

    codes = check("company codes", "list_company_codes", lambda r: f"{r['count']} company codes")
    assert codes and codes["company_codes"], "no company codes visible"
    wanted = os.getenv("S4HC_SMOKE_COMPANY_CODE")
    cc_row = next((c for c in codes["company_codes"] if c["CompanyCode"] == wanted), codes["company_codes"][0])
    company, coa = cc_row["CompanyCode"], cc_row["ChartOfAccounts"]
    print(f"Using company code {company}, chart of accounts {coa}, fiscal year {fy}")

    def no_blank_cost_center(r):
        assert all(c["cost_center"] for c in r["cost_centers"]), "a row without cost center leaked into the totals"
        return f"{len(r['cost_centers'])} cost centers, total {r['grand_total']} {r['currency']}"

    ccs = check("cost centers", "list_cost_centers", lambda r: f"{r['count']} rows from {r['source']}",
                company_code=company, top=50)
    check("profit centers", "list_profit_centers", lambda r: f"{r['count']} rows, assignment from {r['company_code_source']}",
          company_code=company, top=50)
    check("G/L accounts", "list_gl_accounts",
          lambda r: f"{r['count']} P&L accounts, names {'present' if r['gl_accounts'] and r['gl_accounts'][0].get('GLAccountName') else 'missing'}",
          chart_of_accounts=coa, only_pl=True, top=50)
    actuals = check("actuals", "get_cost_center_actuals",
                    lambda r: f"{r['count']} rows, total {r['total']} ({r['completeness']['status']})",
                    company_code=company, fiscal_year=fy, top=200)
    summary = check("cost center summary", "cost_center_summary", no_blank_cost_center,
                    company_code=company, fiscal_year=fy, compare_fiscal_year=fy - 1, top_accounts=5)
    check("breakdown by G/L", "actuals_breakdown", lambda r: f"{r['distinct_values']} accounts, total {r['grand_total']}",
          dimension="GLAccount", company_code=company, fiscal_year=fy, top_n=10)
    check("breakdown by functional area", "actuals_breakdown", lambda r: f"{r['distinct_values']} values",
          dimension="FunctionalArea", company_code=company, fiscal_year=fy, top_n=10)
    check("compare H1 vs H1", "compare_cost_center_periods", lambda r: f"variance {r['variance']}",
          company_code=company, base_fiscal_year=fy, base_period_from=1, base_period_to=6,
          compare_fiscal_year=fy - 1, compare_period_from=1, compare_period_to=6, top_drivers=3)

    busiest = None
    if summary and summary["cost_centers"]:
        busiest = summary["cost_centers"][0]["cost_center"]
    elif ccs and ccs["cost_centers"]:
        busiest = ccs["cost_centers"][0]["CostCenter"]
    if busiest:
        def trend_ok(r):
            if not r["cost_centers"]:
                return "no postings in range"
            s = r["cost_centers"][0]["series"]
            assert [p["period"] for p in s] == sorted(p["period"] for p in s), "periods out of order"
            return f"{len(s)} periods, anomalies {r['cost_centers'][0]['anomalous_periods']}"
        check("trend (cross-year)", "cost_center_trend", trend_ok, company_code=company, cost_centers=[busiest],
              fiscal_year_from=fy - 1, period_from=1, fiscal_year_to=fy, period_to=12)
        check("postings", "get_cost_center_postings", lambda r: f"{r['count']} line items, total {r['total']}",
              cost_center=busiest, company_code=company, fiscal_year=fy, top=20)
    check("CSV export", "export_actuals_csv", lambda r: f"{r['row_count']} rows", company_code=company, fiscal_year=fy, top=50)
    plan = check("plan categories", "get_actual_vs_plan",
                 lambda r: f"{len(r['planning_categories'])} categories", company_code=company, fiscal_year=fy)
    if plan and plan.get("planning_categories"):
        category = max(plan["planning_categories"], key=lambda c: abs(float(c["plan_total"])))["planning_category"]
        check(f"actual vs plan ({category})", "get_actual_vs_plan",
              lambda r: f"actual {r['actual_total']} vs plan {r['plan_total']}",
              company_code=company, fiscal_year=fy, planning_category=category, top_n=5)
    check("describe service", "describe_service", lambda r: f"{len(r['entity_sets'])} entity sets", service="journalentryitem")
    if os.getenv("S4HC_ENABLE_RAW_ODATA", "false").lower() not in ("1", "true", "yes"):
        check("raw OData disabled", "s4hc_odata_read", None, expect_code="DISABLED",
              service="journalentryitem", entity_set="A_CompanyCode")
    if actuals is None:
        print("Actuals failed: check the ledger (S4HC_LEDGER) and that the year has postings.")
    return _print()


def _print() -> int:
    width = max((len(r[1]) for r in results), default=10)
    for status, label, note in results:
        print(f"{status:5} {label:<{width}}  {note}")
    failed = sum(1 for r in results if r[0] == "FAIL")
    print(f"\n{sum(r[0] == 'PASS' for r in results)} passed, {sum(r[0] == 'SKIP' for r in results)} skipped, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
