"""Actual vs plan: API_JOURNALENTRYITEMBASIC_SRV (actuals) against
API_FINPLANNINGENTRYITEM_SRV / A_FinPlanningEntryItem (plan), aligned on the
same ledger, fiscal year, periods, currency basis and dimension.

Plan data only exists here if the customer plans in S/4HANA (or imports SAC
plans into it) under a planning category. If budgets live only in SAC or
elsewhere, this tool says PLAN_UNAVAILABLE rather than inventing a plan.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from fastmcp import Context, FastMCP

from .. import config
from ..client import S4HCError, odata_str, validate_id
from .actuals import envelope, fetch_actuals, pct, resolve_year, totals_by

NAME_FOR = {"CostCenter": "CostCenterName", "GLAccount": "GLAccountName", "ProfitCenter": "ProfitCenterName",
            "FunctionalArea": "FunctionalAreaName", "CompanyCode": "CompanyCodeName"}


def _plan(ctx, *, dims, planning_category, **kw):
    try:
        return fetch_actuals(
            ctx, group_by=dims, service=config.PLAN_SERVICE, entity_set="A_FinPlanningEntryItem",
            allowed_dims=config.PLAN_DIMENSIONS,
            extra_conds=[f"PlanningCategory eq {odata_str(validate_id(planning_category, 'planning_category'))}"]
            if planning_category else None,
            strict=True, **kw,
        )[0]
    except S4HCError as exc:
        if exc.code in ("SAP_FORBIDDEN", "SAP_NOT_FOUND"):
            raise S4HCError(
                "Plan service API_FINPLANNINGENTRYITEM_SRV is not reachable for this communication user "
                "(no arrangement, or plans are not kept in S/4HANA).", "PLAN_UNAVAILABLE") from exc
        raise


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    def get_actual_vs_plan(
        ctx: Context,
        planning_category: str | None = None,
        fiscal_year: int | None = None,
        period_from: int = 1,
        period_to: int = 12,
        dimension: str = "CostCenter",
        cost_centers: list[str] | None = None,
        gl_accounts: list[str] | None = None,
        company_code: str | None = None,
        controlling_area: str | None = None,
        ledger: str | None = None,
        currency: str = "company",
        top_n: int = 50,
    ) -> dict:
        """Actual vs plan per dimension (CostCenter by default, or GLAccount,
        ProfitCenter, FunctionalArea, ...): actual, plan, variance =
        actual − plan, consumption % = actual / plan, coverage of keys that
        exist only on one side. Call WITHOUT planning_category first to list
        the plan categories that hold data for the scope (e.g. PLN, PLN01) —
        never assume one. "actual_above_plan" is neutral: for costs it means
        over plan, for revenue accounts it means ahead of plan.
        """
        fy = resolve_year(fiscal_year)
        common = dict(currency=currency, fiscal_year=fy, period_from=period_from, period_to=period_to, ledger=ledger,
                      company_code=company_code, controlling_area=controlling_area,
                      cost_centers=cost_centers, gl_accounts=gl_accounts)
        if not planning_category:
            rows = _plan(ctx, dims=["PlanningCategory"], planning_category=None, **common)
            cats = totals_by(rows, "PlanningCategory")
            if not cats:
                raise S4HCError(f"No plan data in {fy} for this scope", "PLAN_UNAVAILABLE")
            return {**envelope(f"{config.PLAN_SERVICE}/A_FinPlanningEntryItem"),
                    "fiscal_year": fy, "periods": [period_from, period_to],
                    "planning_categories": [{"planning_category": k, "plan_total": str(v)} for k, v in sorted(cats.items())],
                    "next_step": "Call again with planning_category set to one of these."}

        if dimension not in config.PLAN_DIMENSIONS or dimension == "PlanningCategory":
            raise S4HCError(f"dimension must be one of {config.PLAN_DIMENSIONS[:-1]}", "UNSUPPORTED_FIELD")
        dims = [dimension] + ([NAME_FOR[dimension]] if dimension in NAME_FOR else [])
        actual_rows, _ = fetch_actuals(ctx, group_by=dims, strict=True, **common)
        plan_rows = _plan(ctx, dims=dims, planning_category=planning_category, **common)
        if not plan_rows:
            raise S4HCError(f"Planning category {planning_category!r} has no data in {fy} for this scope", "PLAN_UNAVAILABLE")
        curs = {r.get("Currency") for r in actual_rows + plan_rows if r.get("Currency")}
        if len(curs) > 1:
            raise S4HCError(f"Actual and plan are in different currencies {sorted(curs)}", "MIXED_CURRENCY")

        a, p = totals_by(actual_rows, dimension), totals_by(plan_rows, dimension)
        names = {str(r.get(dimension)): r.get(NAME_FOR.get(dimension, "")) for r in actual_rows + plan_rows}
        items: list[dict[str, Any]] = []
        for k in set(a) | set(p):
            av, pv = a.get(k, Decimal(0)), p.get(k, Decimal(0))
            items.append({dimension: k, "name": names.get(k), "actual": str(av), "plan": str(pv),
                          "variance": str(av - pv), "variance_pct": pct(av, pv),
                          "consumption_pct": str(round(av / pv * 100, 1)) if pv else None,
                          "actual_above_plan": av > pv, "only_in": "actual" if k not in p else ("plan" if k not in a else None)})
        items.sort(key=lambda x: -abs(Decimal(x["variance"])))
        ta, tp = sum(a.values(), Decimal(0)), sum(p.values(), Decimal(0))
        only_actual = sorted(k for k in a if k not in p)
        warnings = [f"{len(only_actual)} {dimension} value(s) have actuals but no plan: {only_actual[:10]}"] if only_actual else []
        return {
            **envelope(f"{config.JOURNAL_ITEM_SERVICE} + {config.PLAN_SERVICE}", warnings=warnings),
            "fiscal_year": fy, "periods": [period_from, period_to], "planning_category": planning_category,
            "dimension": dimension, "currency": sorted(curs),
            "actual_total": str(ta), "plan_total": str(tp), "variance": str(ta - tp), "variance_pct": pct(ta, tp),
            "consumption_pct": str(round(ta / tp * 100, 1)) if tp else None,
            "items": items[: max(1, top_n)], "distinct_values": len(items),
        }
