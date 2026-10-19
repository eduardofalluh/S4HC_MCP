"""Actuals from API_JOURNALENTRYITEMBASIC_SRV / A_JournalEntryItemBasic.

A_JournalEntryItemBasic is an aggregating (analytical) entity: SAP sums the
amount measures over whatever dimensions are in $select. So "group by" is
pushed to S/4HANA and the MCP only ever receives aggregated rows.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from fastmcp import Context, FastMCP

from .. import config
from ..client import S4HCError, odata_str, validate_fields, validate_id
from ..dependencies import get_client, get_defaults
from ..governance import allowed_company_codes_for_filter, enforce_company_code, enforce_cost_centers

AMOUNT_BY_CURRENCY = {
    "company": ("AmountInCompanyCodeCurrency", "CompanyCodeCurrency"),
    "global": ("AmountInGlobalCurrency", "GlobalCurrency"),
    "transaction": ("AmountInTransactionCurrency", "TransactionCurrency"),
}
DEFAULT_DIMS = ["CostCenter", "CostCenterName", "GLAccount", "GLAccountName", "FiscalPeriod"]
LIST_FILTERS = {  # tool argument -> OData field
    "cost_centers": "CostCenter",
    "profit_centers": "ProfitCenter",
    "gl_accounts": "GLAccount",
    "functional_areas": "FunctionalArea",
    "projects": "Project",
    "wbs_elements": "WBSElement",
    "orders": "OrderID",
}


def dec(v: Any) -> Decimal:
    try:
        return Decimal(str(v)) if v not in (None, "") else Decimal(0)
    except InvalidOperation:
        return Decimal(0)


def period(p: int) -> str:
    return f"{int(p):03d}"


def resolve_year(fiscal_year: int | None) -> int:
    """Default to the current calendar year (fiscal year variant K4)."""
    return int(fiscal_year) if fiscal_year else date.today().year


def build_filter(
    *,
    ledger: str,
    fiscal_year: int,
    period_from: int,
    period_to: int,
    company_code: str | None = None,
    controlling_area: str | None = None,
    lists: dict[str, list[str] | None] | None = None,
    company_codes_any: list[str] | None = None,
) -> str:
    if not 0 <= period_from <= period_to <= 16:
        raise S4HCError("period_from/period_to must satisfy 0 <= from <= to <= 16 "
                        "(000 = balance carryforward, 013-016 = special periods)", "INVALID_SCOPE")
    conds = [
        f"Ledger eq {odata_str(validate_id(ledger, 'ledger'))}",
        f"LedgerFiscalYear eq {odata_str(str(int(fiscal_year)))}",
        f"FiscalPeriod ge {odata_str(period(period_from))} and FiscalPeriod le {odata_str(period(period_to))}",
    ]
    if company_code:
        conds.append(f"CompanyCode eq {odata_str(validate_id(company_code, 'company_code'))}")
    elif company_codes_any:
        conds.append("(" + " or ".join(f"CompanyCode eq {odata_str(validate_id(c, 'company_code'))}" for c in company_codes_any) + ")")
    if controlling_area:
        conds.append(f"ControllingArea eq {odata_str(validate_id(controlling_area, 'controlling_area'))}")
    for arg, field in LIST_FILTERS.items():
        values = (lists or {}).get(arg)
        if values:
            ids = [validate_id(v, arg) for v in values]
            if len(ids) > 200:
                raise S4HCError(f"{arg}: at most 200 values per call", "QUERY_TOO_LARGE")
            conds.append("(" + " or ".join(f"{field} eq {odata_str(i)}" for i in ids) + ")")
    return " and ".join(conds)


def fetch_actuals(
    ctx: Context | None,
    *,
    group_by: list[str],
    currency: str,
    fiscal_year: int,
    period_from: int,
    period_to: int,
    ledger: str | None = None,
    company_code: str | None = None,
    controlling_area: str | None = None,
    top: int | None = None,
    strict: bool = False,
    service: str | None = None,
    entity_set: str = "A_JournalEntryItemBasic",
    allowed_dims: list[str] | None = None,
    extra_conds: list[str] | None = None,
    **lists: list[str] | None,
) -> tuple[list[dict], bool]:
    """Fetch aggregated rows. Each row gets Amount (str Decimal) and Currency.

    strict=True is for tools that compute totals: a truncated result raises
    QUERY_TOO_LARGE and more than one currency raises MIXED_CURRENCY, so a
    partial or cross-currency sum is never presented as a complete total.
    """
    if currency not in AMOUNT_BY_CURRENCY:
        raise S4HCError(f"currency must be one of {list(AMOUNT_BY_CURRENCY)}", "INVALID_SCOPE")
    amount, cur = AMOUNT_BY_CURRENCY[currency]
    d = get_defaults(ctx)
    # Backend-enforced data scope (allowlists) — never left to the agent.
    company_code = enforce_company_code(ctx, company_code)
    lists = dict(lists)
    lists["cost_centers"] = enforce_cost_centers(ctx, lists.get("cost_centers"))
    dims = validate_fields(group_by, allowed_dims or config.JOURNAL_ITEM_DIMENSIONS, entity_set)
    if cur not in dims:
        dims.append(cur)
    flt = build_filter(
        ledger=ledger or d.ledger, fiscal_year=fiscal_year, period_from=period_from, period_to=period_to,
        company_code=company_code, controlling_area=controlling_area or d.controlling_area, lists=lists,
        company_codes_any=allowed_company_codes_for_filter(ctx, company_code),
    )
    if extra_conds:
        flt = " and ".join([flt, *extra_conds])
    rows, truncated = get_client(ctx).get_all(
        service or config.JOURNAL_ITEM_SERVICE, entity_set,
        select=dims + [amount], filter_=flt,
        top=top if not strict else None,
    )
    for r in rows:
        r["Amount"] = str(dec(r.pop(amount, None)))
        r["Currency"] = r.pop(cur, None)
    if strict:
        if truncated:
            raise S4HCError(
                f"Result exceeds S4HC_MAX_ROWS ({config.get_max_rows()}); a total over it would be incomplete. "
                "Narrow the scope (company code, cost centers, periods) or group by fewer dimensions.",
                "QUERY_TOO_LARGE")
        currencies = {r["Currency"] for r in rows if r.get("Currency")}
        if len(currencies) > 1:
            raise S4HCError(
                f"Scope spans several currencies {sorted(currencies)}; amounts cannot be summed. "
                "Filter to one company_code or use currency='global'.", "MIXED_CURRENCY")
    return rows, truncated


def envelope(source: str, *, truncated: bool = False, warnings: list[str] | None = None, next_hint: str | None = None) -> dict:
    """Standard provenance + completeness block attached to every analytical result."""
    status = "partial" if truncated else "complete"
    w = list(warnings or [])
    if truncated:
        w.append("Rows were capped; totals in this response cover only the returned rows.")
    return {
        "provenance": {"source_api": source, "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds")},
        "completeness": {"status": status, "warnings": w, **({"hint": next_hint} if next_hint else {})},
    }


def totals_per_currency(rows: list[dict]) -> dict[str, str]:
    acc: dict[str, Decimal] = defaultdict(Decimal)
    for r in rows:
        acc[str(r.get("Currency"))] += dec(r["Amount"])
    return {k: str(v) for k, v in sorted(acc.items())}


def totals_by(rows: list[dict], key: str) -> dict[str, Decimal]:
    acc: dict[str, Decimal] = defaultdict(Decimal)
    for r in rows:
        acc[str(r.get(key))] += dec(r["Amount"])
    return dict(acc)


def pct(cur: Decimal, prev: Decimal) -> str | None:
    """Variance % against |baseline|; None when the baseline is zero (no meaningful %).
    Using |baseline| keeps the sign = direction of change even for negative
    (credit) baselines; agree this convention with Finance."""
    return str(round((cur - prev) / abs(prev) * 100, 1)) if prev else None


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    def get_cost_center_actuals(
        ctx: Context,
        fiscal_year: int | None = None,
        cost_centers: list[str] | None = None,
        period_from: int = 1,
        period_to: int = 12,
        ledger: str | None = None,
        company_code: str | None = None,
        controlling_area: str | None = None,
        gl_accounts: list[str] | None = None,
        profit_centers: list[str] | None = None,
        functional_areas: list[str] | None = None,
        group_by: list[str] | None = None,
        currency: str = "company",
        top: int = 5000,
    ) -> dict:
        """Actual postings from the Universal Journal (ACDOCA) via
        API_JOURNALENTRYITEMBASIC_SRV, aggregated by S/4HANA over group_by.
        fiscal_year defaults to the current year. Default group_by =
        [CostCenter, CostCenterName, GLAccount, GLAccountName, FiscalPeriod]; any
        A_JournalEntryItemBasic dimension is allowed (ProfitCenter, FunctionalArea,
        CostCtrActivityType, Project, WBSElement, PartnerCostCenter, OrderID, ...).
        Sign convention is SAP's: costs/debits positive, credits negative.
        currency = company | global | transaction. Returns {rows, count, truncated, total, filter}.
        """
        fy = resolve_year(fiscal_year)
        rows, truncated = fetch_actuals(
            ctx, group_by=group_by or DEFAULT_DIMS, currency=currency, fiscal_year=fy,
            period_from=period_from, period_to=period_to, ledger=ledger, company_code=company_code,
            controlling_area=controlling_area, top=max(1, min(top, config.get_max_rows())),
            cost_centers=cost_centers, gl_accounts=gl_accounts, profit_centers=profit_centers,
            functional_areas=functional_areas,
        )
        per_cur = totals_per_currency(rows)
        total = next(iter(per_cur.values())) if len(per_cur) == 1 else None
        warn = [] if total is not None or not rows else [f"Several currencies {sorted(per_cur)}: see totals_by_currency; no single total."]
        return {
            **envelope(f"{config.JOURNAL_ITEM_SERVICE}/A_JournalEntryItemBasic", truncated=truncated, warnings=warn,
                       next_hint="Narrow the filter or raise top" if truncated else None),
            "count": len(rows), "truncated": truncated, "total": total, "totals_by_currency": per_cur,
            "filter": {"fiscal_year": fy, "periods": [period_from, period_to],
                       "ledger": ledger or get_defaults(ctx).ledger, "cost_centers": cost_centers,
                       "profit_centers": profit_centers, "gl_accounts": gl_accounts},
            "rows": rows,
        }

    @mcp.tool()
    def cost_center_summary(
        ctx: Context,
        fiscal_year: int | None = None,
        cost_centers: list[str] | None = None,
        compare_fiscal_year: int | None = None,
        period_from: int = 1,
        period_to: int = 12,
        ledger: str | None = None,
        company_code: str | None = None,
        controlling_area: str | None = None,
        currency: str = "company",
        top_accounts: int = 15,
    ) -> dict:
        """Ready-to-analyze cost center picture: total per cost center, spend by
        period, top G/L accounts, and (optionally) variance vs compare_fiscal_year
        for the same periods. Everything is aggregated in S/4HANA; the LLM only
        sees summaries. Use get_cost_center_actuals or actuals_breakdown for
        custom cuts.
        """
        fy = resolve_year(fiscal_year)

        def pull(year: int) -> list[dict]:
            rows, _ = fetch_actuals(
                ctx, group_by=DEFAULT_DIMS, currency=currency, fiscal_year=year,
                period_from=period_from, period_to=period_to, ledger=ledger, company_code=company_code,
                controlling_area=controlling_area, cost_centers=cost_centers, strict=True,
            )
            return rows

        cur_rows = pull(fy)
        cmp_rows = pull(compare_fiscal_year) if compare_fiscal_year else []

        by_cc, by_cc_cmp = totals_by(cur_rows, "CostCenter"), totals_by(cmp_rows, "CostCenter")
        names = {str(r.get("CostCenter")): r.get("CostCenterName") for r in cur_rows}
        gl_names = {str(r.get("GLAccount")): r.get("GLAccountName") for r in cur_rows + cmp_rows}
        currencies = sorted({r.get("Currency") for r in cur_rows if r.get("Currency")})

        cost_center_block = []
        for cc, amt in sorted(by_cc.items(), key=lambda kv: -abs(kv[1])):
            item: dict[str, Any] = {"cost_center": cc, "name": names.get(cc), "total": str(amt)}
            if compare_fiscal_year:
                prev = by_cc_cmp.get(cc, Decimal(0))
                item.update(compare_total=str(prev), variance=str(amt - prev), variance_pct=pct(amt, prev))
            cost_center_block.append(item)

        by_period, by_period_cmp = totals_by(cur_rows, "FiscalPeriod"), totals_by(cmp_rows, "FiscalPeriod")
        period_block = [
            {"period": p, "total": str(by_period.get(p, Decimal(0))),
             **({"compare_total": str(by_period_cmp.get(p, Decimal(0)))} if compare_fiscal_year else {})}
            for p in sorted(set(by_period) | set(by_period_cmp))
        ]

        by_gl, by_gl_cmp = totals_by(cur_rows, "GLAccount"), totals_by(cmp_rows, "GLAccount")
        gl_block = []
        for gl, amt in sorted(by_gl.items(), key=lambda kv: -abs(kv[1]))[: max(1, top_accounts)]:
            item = {"gl_account": gl, "name": gl_names.get(gl), "total": str(amt)}
            if compare_fiscal_year:
                prev = by_gl_cmp.get(gl, Decimal(0))
                item.update(compare_total=str(prev), variance=str(amt - prev), variance_pct=pct(amt, prev))
            gl_block.append(item)

        grand = sum(by_cc.values(), Decimal(0))
        out: dict[str, Any] = {
            **envelope(f"{config.JOURNAL_ITEM_SERVICE}/A_JournalEntryItemBasic"),
            "fiscal_year": fy, "periods": [period_from, period_to],
            "ledger": ledger or get_defaults(ctx).ledger, "currency": currencies,
            "grand_total": str(grand),
            "cost_centers": cost_center_block, "by_period": period_block,
            "top_gl_accounts": gl_block, "source_rows": len(cur_rows),
        }
        if compare_fiscal_year:
            grand_cmp = sum(by_cc_cmp.values(), Decimal(0))
            out.update(compare_fiscal_year=compare_fiscal_year, compare_grand_total=str(grand_cmp),
                       grand_variance=str(grand - grand_cmp), grand_variance_pct=pct(grand, grand_cmp))
        return out
