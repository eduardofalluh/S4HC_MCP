"""Analysis helpers on top of the aggregated actuals: breakdowns by any
dimension, cross-year trends with anomaly flags, CSV export, fiscal context."""

from __future__ import annotations

import csv
import io
import statistics
from datetime import date
from decimal import Decimal
from typing import Any

from fastmcp import Context, FastMCP

from .. import config
from ..client import S4HCError
from ..dependencies import get_defaults
from .actuals import DEFAULT_DIMS, dec, envelope, fetch_actuals, pct, resolve_year, totals_by

NAME_FOR = {
    "CostCenter": "CostCenterName", "ProfitCenter": "ProfitCenterName", "GLAccount": "GLAccountName",
    "FunctionalArea": "FunctionalAreaName", "CompanyCode": "CompanyCodeName", "BusinessArea": "BusinessAreaName",
    "Segment": "SegmentName", "PartnerCostCenter": "PartnerCostCenterName",
    "PartnerProfitCenter": "PartnerProfitCenterName", "Plant": "PlantName", "Customer": "CustomerName",
    "CostCtrActivityType": "CostCtrActivityTypeName", "Project": "ProjectDescription",
    "WBSElement": "WBSElementDescription", "Ledger": "LedgerName",
}


def _year_period_range(fy_from: int, p_from: int, fy_to: int, p_to: int) -> list[tuple[int, int, int]]:
    """Split a cross-year range into per-year (year, period_from, period_to) chunks."""
    if (fy_from, p_from) > (fy_to, p_to):
        raise S4HCError("from must be before to", "INVALID_SCOPE")
    if fy_to - fy_from > 3:
        raise S4HCError("trend range limited to 4 fiscal years", "QUERY_TOO_LARGE")
    chunks = []
    for y in range(fy_from, fy_to + 1):
        chunks.append((y, p_from if y == fy_from else 1, p_to if y == fy_to else 12))
    return chunks


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    def fiscal_context(ctx: Context, company_code: str | None = None) -> dict:
        """Today's date, the default fiscal year/period the other tools assume
        (calendar-year variant K4: period = month), the default ledger, language
        and controlling area in effect for this connection. Pass company_code to
        check its actual fiscal year variant — if it is not K4, "period 1" is
        NOT January and the warning says so. Call it when the user says
        "this year", "last month", "YTD".
        """
        today = date.today()
        d = get_defaults(ctx)
        fyv, warnings = None, []
        if company_code:
            from .. import config as _cfg
            from ..client import odata_str, validate_id
            from ..dependencies import get_client
            from ..governance import enforce_company_code
            cc = enforce_company_code(ctx, company_code)
            rows, _ = get_client(ctx).get_all(_cfg.JOURNAL_ITEM_SERVICE, "A_CompanyCode",
                                              select=["CompanyCode", "FiscalYearVariant", "Currency"],
                                              filter_=f"CompanyCode eq {odata_str(validate_id(cc, 'company_code'))}", top=1)
            fyv = rows[0].get("FiscalYearVariant") if rows else None
            if fyv and fyv != "K4":
                warnings.append(f"Company code {cc} uses fiscal year variant {fyv}: periods are not calendar months; "
                                "fiscal_year/fiscal_period below are calendar-based and must be shifted.")
        prev_y, prev_p = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
        return {
            "today": today.isoformat(), "fiscal_year": today.year, "fiscal_period": today.month,
            "previous_period": {"fiscal_year": prev_y, "fiscal_period": prev_p},
            "ytd_periods": [1, today.month], "assumed_fiscal_year_variant": "K4 (calendar year)",
            "note": "If the company code uses a non-calendar FYV (see list_company_codes.FiscalYearVariant), shift periods accordingly.",
            "default_ledger": d.ledger, "language": d.language, "controlling_area": d.controlling_area,
            **({"company_code_fiscal_year_variant": fyv} if company_code else {}), "warnings": warnings,
        }

    @mcp.tool()
    def actuals_breakdown(
        ctx: Context,
        dimension: str = "CostCenter",
        fiscal_year: int | None = None,
        compare_fiscal_year: int | None = None,
        period_from: int = 1,
        period_to: int = 12,
        cost_centers: list[str] | None = None,
        profit_centers: list[str] | None = None,
        gl_accounts: list[str] | None = None,
        functional_areas: list[str] | None = None,
        company_code: str | None = None,
        controlling_area: str | None = None,
        ledger: str | None = None,
        currency: str = "company",
        top_n: int = 50,
    ) -> dict:
        """Totals by ANY single dimension (CostCenter, ProfitCenter, GLAccount,
        FunctionalArea, CompanyCode, Segment, Project, WBSElement, OrderID,
        CostCtrActivityType, PartnerCostCenter, ...) with share of total and
        optional variance vs compare_fiscal_year. Filter with the *_s lists.
        Example: dimension='GLAccount', cost_centers=['4120'] → what cost
        center 4120 spends on. Example: dimension='CostCenter',
        gl_accounts=['65000000'] → who books travel.
        """
        if dimension not in config.JOURNAL_ITEM_DIMENSIONS:
            raise S4HCError(f"dimension must be one of {config.JOURNAL_ITEM_DIMENSIONS}", "UNSUPPORTED_FIELD")
        fy = resolve_year(fiscal_year)
        dims = [dimension] + ([NAME_FOR[dimension]] if dimension in NAME_FOR else [])

        def pull(year: int) -> list[dict]:
            rows, _ = fetch_actuals(
                ctx, group_by=dims, currency=currency, fiscal_year=year, period_from=period_from,
                period_to=period_to, ledger=ledger, company_code=company_code, controlling_area=controlling_area,
                cost_centers=cost_centers, profit_centers=profit_centers, gl_accounts=gl_accounts,
                functional_areas=functional_areas, strict=True,
            )
            return rows

        cur, cmp_ = pull(fy), (pull(compare_fiscal_year) if compare_fiscal_year else [])
        by, by_cmp = totals_by(cur, dimension), totals_by(cmp_, dimension)
        names = {str(r.get(dimension)): r.get(NAME_FOR.get(dimension, "")) for r in cur + cmp_}
        grand = sum(by.values(), Decimal(0))
        items = []
        for k, amt in sorted(by.items(), key=lambda kv: -abs(kv[1]))[: max(1, top_n)]:
            it: dict[str, Any] = {dimension: k, "name": names.get(k), "total": str(amt),
                                  "share_pct": str(round(amt / grand * 100, 1)) if grand else None}
            if compare_fiscal_year:
                prev = by_cmp.get(k, Decimal(0))
                it.update(compare_total=str(prev), variance=str(amt - prev), variance_pct=pct(amt, prev))
            items.append(it)
        out: dict[str, Any] = {
            **envelope(f"{config.JOURNAL_ITEM_SERVICE}/A_JournalEntryItemBasic"),
            "dimension": dimension, "fiscal_year": fy, "periods": [period_from, period_to],
            "currency": sorted({r.get("Currency") for r in cur if r.get("Currency")}),
            "grand_total": str(grand), "distinct_values": len(by), "shown": len(items), "items": items,
        }
        if compare_fiscal_year:
            g2 = sum(by_cmp.values(), Decimal(0))
            out.update(compare_fiscal_year=compare_fiscal_year, compare_grand_total=str(g2),
                       grand_variance=str(grand - g2), grand_variance_pct=pct(grand, g2))
        return out

    @mcp.tool()
    def compare_cost_center_periods(
        ctx: Context,
        base_fiscal_year: int,
        base_period_from: int,
        base_period_to: int,
        compare_fiscal_year: int,
        compare_period_from: int,
        compare_period_to: int,
        cost_centers: list[str] | None = None,
        company_code: str | None = None,
        controlling_area: str | None = None,
        ledger: str | None = None,
        currency: str = "company",
        top_drivers: int = 10,
    ) -> dict:
        """Compare any two period ranges (Q3 vs Q2, this month vs same month
        last year, H1 2026 vs H1 2025): per cost center variance, and for each
        cost center the G/L accounts that drive the change. Deterministic math
        in the backend; answers "which cost centers increased the most and
        which accounts explain it".
        """
        def pull(y, pf, pt):
            rows, _ = fetch_actuals(
                ctx, group_by=["CostCenter", "CostCenterName", "GLAccount", "GLAccountName"], currency=currency,
                fiscal_year=y, period_from=pf, period_to=pt, ledger=ledger, company_code=company_code,
                controlling_area=controlling_area, cost_centers=cost_centers, strict=True,
            )
            return rows

        base, comp = pull(base_fiscal_year, base_period_from, base_period_to), pull(compare_fiscal_year, compare_period_from, compare_period_to)
        names = {str(r["CostCenter"]): r.get("CostCenterName") for r in base + comp}
        gl_names = {str(r["GLAccount"]): r.get("GLAccountName") for r in base + comp}

        def by_cc_gl(rows):
            acc: dict[str, dict[str, Decimal]] = {}
            for r in rows:
                acc.setdefault(str(r["CostCenter"]), {})
                acc[str(r["CostCenter"])][str(r["GLAccount"])] = acc[str(r["CostCenter"])].get(str(r["GLAccount"]), Decimal(0)) + dec(r["Amount"])
            return acc

        b, c = by_cc_gl(base), by_cc_gl(comp)
        items = []
        for cc in sorted(set(b) | set(c)):
            bt, ct = sum(b.get(cc, {}).values(), Decimal(0)), sum(c.get(cc, {}).values(), Decimal(0))
            drivers = []
            for gl in set(b.get(cc, {})) | set(c.get(cc, {})):
                d = b.get(cc, {}).get(gl, Decimal(0)) - c.get(cc, {}).get(gl, Decimal(0))
                if d:
                    drivers.append({"gl_account": gl, "name": gl_names.get(gl), "base": str(b.get(cc, {}).get(gl, Decimal(0))),
                                    "compare": str(c.get(cc, {}).get(gl, Decimal(0))), "variance": str(d)})
            drivers.sort(key=lambda x: -abs(Decimal(x["variance"])))
            items.append({"cost_center": cc, "name": names.get(cc), "base_total": str(bt), "compare_total": str(ct),
                          "variance": str(bt - ct), "variance_pct": pct(bt, ct), "drivers": drivers[: max(1, top_drivers)]})
        items.sort(key=lambda x: -abs(Decimal(x["variance"])))
        gb, gc = sum((Decimal(i["base_total"]) for i in items), Decimal(0)), sum((Decimal(i["compare_total"]) for i in items), Decimal(0))
        cur_set = {r.get("Currency") for r in base + comp if r.get("Currency")}
        if len(cur_set) > 1:
            raise S4HCError(f"Base and comparison are in different currencies {sorted(cur_set)}", "MIXED_CURRENCY")
        # Driver contributions reconcile exactly to the total change (no residual: every G/L line is included).
        return {**envelope(f"{config.JOURNAL_ITEM_SERVICE}/A_JournalEntryItemBasic"),
                "base": {"fiscal_year": base_fiscal_year, "periods": [base_period_from, base_period_to], "total": str(gb)},
                "compare": {"fiscal_year": compare_fiscal_year, "periods": [compare_period_from, compare_period_to], "total": str(gc)},
                "variance": str(gb - gc), "variance_pct": pct(gb, gc),
                "currency": sorted({r.get("Currency") for r in base if r.get("Currency")}), "cost_centers": items}

    @mcp.tool()
    def cost_center_trend(
        ctx: Context,
        cost_centers: list[str] | None = None,
        fiscal_year_from: int | None = None,
        period_from: int = 1,
        fiscal_year_to: int | None = None,
        period_to: int = 12,
        gl_accounts: list[str] | None = None,
        company_code: str | None = None,
        controlling_area: str | None = None,
        ledger: str | None = None,
        currency: str = "company",
        anomaly_threshold_pct: float = 30.0,
    ) -> dict:
        """Period-by-period series per cost center across a range that may span
        fiscal years (e.g. 2025/007 → 2026/006 = trailing 12 months). Returns
        month-over-month change and flags periods whose amount deviates more
        than anomaly_threshold_pct from the cost center's average, plus a z-score.
        Use for "what spiked", "trend", "run-rate" questions.
        """
        fy_to = resolve_year(fiscal_year_to)
        fy_from = int(fiscal_year_from) if fiscal_year_from else fy_to
        rows: list[dict] = []
        for y, pf, pt in _year_period_range(fy_from, period_from, fy_to, period_to):
            chunk, _ = fetch_actuals(
                ctx, group_by=["CostCenter", "CostCenterName", "LedgerFiscalYear", "FiscalPeriod"],
                currency=currency, fiscal_year=y, period_from=pf, period_to=pt, ledger=ledger,
                company_code=company_code, controlling_area=controlling_area,
                cost_centers=cost_centers, gl_accounts=gl_accounts, strict=True,
            )
            rows.extend(chunk)
        if len({r.get("Currency") for r in rows if r.get("Currency")}) > 1:
            raise S4HCError("Trend range spans several currencies; filter to one company_code or use currency='global'", "MIXED_CURRENCY")

        series: dict[str, dict[str, Decimal]] = {}
        names: dict[str, Any] = {}
        for r in rows:
            cc = str(r.get("CostCenter"))
            key = f"{r.get('LedgerFiscalYear')}/{r.get('FiscalPeriod')}"
            series.setdefault(cc, {})
            series[cc][key] = series[cc].get(key, Decimal(0)) + dec(r["Amount"])
            names[cc] = r.get("CostCenterName")

        out_cc = []
        for cc, s in sorted(series.items()):
            keys = sorted(s)
            vals = [s[k] for k in keys]
            avg = sum(vals, Decimal(0)) / len(vals) if vals else Decimal(0)
            sd = Decimal(str(statistics.pstdev([float(v) for v in vals]))) if len(vals) > 1 else Decimal(0)
            points, anomalies, prev = [], [], None
            for k, v in zip(keys, vals):
                dev = pct(v, avg) if avg else None
                z = str(round((v - avg) / sd, 2)) if sd else None
                pt = {"period": k, "amount": str(v), "mom_change": str(v - prev) if prev is not None else None,
                      "mom_change_pct": pct(v, prev) if prev is not None else None, "vs_avg_pct": dev, "z": z}
                if dev is not None and abs(float(dev)) >= anomaly_threshold_pct:
                    pt["anomaly"] = True
                    anomalies.append(k)
                points.append(pt)
                prev = v
            out_cc.append({"cost_center": cc, "name": names.get(cc), "total": str(sum(vals, Decimal(0))),
                           "average_per_period": str(round(avg, 2)), "periods": len(vals),
                           "anomalous_periods": anomalies, "series": points})
        return {**envelope(f"{config.JOURNAL_ITEM_SERVICE}/A_JournalEntryItemBasic", warnings=[
                    "Anomaly flags are descriptive (deviation from the cost center's own average), not evidence of an accounting error."]),
                "range": {"from": f"{fy_from}/{period_from:03d}", "to": f"{fy_to}/{period_to:03d}"},
                "currency": sorted({r.get("Currency") for r in rows if r.get("Currency")}),
                "anomaly_threshold_pct": anomaly_threshold_pct, "cost_centers": out_cc}

    @mcp.tool()
    def export_actuals_csv(
        ctx: Context,
        fiscal_year: int | None = None,
        cost_centers: list[str] | None = None,
        period_from: int = 1,
        period_to: int = 12,
        group_by: list[str] | None = None,
        gl_accounts: list[str] | None = None,
        profit_centers: list[str] | None = None,
        company_code: str | None = None,
        controlling_area: str | None = None,
        ledger: str | None = None,
        currency: str = "company",
        top: int = 20000,
    ) -> dict:
        """Same data as get_cost_center_actuals but returned as CSV text
        (header + rows) for the agent to hand back as a file or paste into Excel.
        """
        fy = resolve_year(fiscal_year)
        dims = group_by or DEFAULT_DIMS
        rows, truncated = fetch_actuals(
            ctx, group_by=dims, currency=currency, fiscal_year=fy, period_from=period_from,
            period_to=period_to, ledger=ledger, company_code=company_code, controlling_area=controlling_area,
            top=max(1, min(top, config.get_max_rows())), cost_centers=cost_centers, gl_accounts=gl_accounts,
            profit_centers=profit_centers,
        )
        cols = [c for c in dims if c in config.JOURNAL_ITEM_DIMENSIONS] + ["Amount", "Currency"]
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
        return {**envelope(f"{config.JOURNAL_ITEM_SERVICE}/A_JournalEntryItemBasic", truncated=truncated),
                "rows": len(rows), "truncated": truncated, "columns": cols,
                "filename": f"cost_center_actuals_{fy}_{period_from:03d}-{period_to:03d}.csv", "csv": buf.getvalue()}
