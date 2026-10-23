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
from ..client import S4HCError, odata_str, validate_id
from ..dependencies import get_client, get_defaults
from ..governance import enforce_company_code
from .actuals import (DEFAULT_DIMS, dec, ensure_single_currency, envelope, fetch_actuals, inline_top, pct, period,
                      resolve_year, totals_by)

NAME_FOR = config.NAME_FOR
MIN_PERIODS_FOR_ANOMALY = 4
_CSV_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def _year_period_range(fy_from: int, p_from: int, fy_to: int, p_to: int) -> list[tuple[int, int, int]]:
    """Split a cross-year range into per-year (year, period_from, period_to) chunks."""
    if (fy_from, p_from) > (fy_to, p_to):
        raise S4HCError("from must be before to", "INVALID_SCOPE")
    if fy_to - fy_from > 3:
        raise S4HCError("trend range limited to 4 fiscal years", "QUERY_TOO_LARGE")
    if not (1 <= p_from <= 16 and 1 <= p_to <= 16):
        raise S4HCError("period_from/period_to must be between 1 and 16", "INVALID_SCOPE")
    chunks = []
    for y in range(fy_from, fy_to + 1):
        chunks.append((y, p_from if y == fy_from else 1, p_to if y == fy_to else 12))
    return chunks


def _csv_safe(value: Any) -> Any:
    """Neutralise text that Excel would run as a formula (CSV injection).
    Amount is numeric and written as-is; this only touches SAP text cells."""
    if isinstance(value, str) and value.startswith(_CSV_FORMULA_START):
        return "'" + value
    return value


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
            cc = enforce_company_code(ctx, validate_id(company_code, "company_code"))
            rows, _ = get_client(ctx).get_all(config.JOURNAL_ITEM_SERVICE, "A_CompanyCode",
                                              select=["CompanyCode", "FiscalYearVariant", "Currency"],
                                              filter_=f"CompanyCode eq {odata_str(cc)}", top=1)
            if not rows:
                warnings.append(f"Company code {cc} does not exist on this tenant; call list_company_codes.")
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
        cost_center_lines_only: bool = True,
        top_n: int = 50,
    ) -> dict:
        """Totals by ANY single dimension (CostCenter, ProfitCenter, GLAccount,
        FunctionalArea, CompanyCode, Segment, BusinessArea, Plant, Project,
        WBSElement, OrderID, CostCtrActivityType, PartnerCostCenter, ...) with
        share of total and optional variance vs compare_fiscal_year. Filter with
        the *_s lists. Only lines posted to a cost center count unless
        cost_center_lines_only=false (use false for e.g. revenue by profit
        center). Example: dimension='GLAccount', cost_centers=['17101101'] → what
        that cost center spends on. Example: dimension='CostCenter',
        gl_accounts=['65008430'] → who books on that account.
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
                functional_areas=functional_areas, strict=True, cost_center_lines_only=cost_center_lines_only,
            )
            return rows

        cur, cmp_ = pull(fy), (pull(compare_fiscal_year) if compare_fiscal_year else [])
        currencies = ensure_single_currency(cur, cmp_, what="The two years")
        by, by_cmp = totals_by(cur, dimension), totals_by(cmp_, dimension)
        names = {str(r.get(dimension)): r.get(NAME_FOR.get(dimension, "")) for r in cmp_ + cur}
        grand = sum(by.values(), Decimal(0))
        items = []
        keys = set(by) | set(by_cmp)
        for k in sorted(keys, key=lambda k: -abs(by.get(k, Decimal(0))))[: max(1, top_n)]:
            amt = by.get(k, Decimal(0))
            it: dict[str, Any] = {dimension: k, "name": names.get(k), "total": str(amt),
                                  "share_pct": str(round(amt / grand * 100, 1)) if grand else None}
            if compare_fiscal_year:
                prev = by_cmp.get(k, Decimal(0))
                it.update(compare_total=str(prev), variance=str(amt - prev), variance_pct=pct(amt, prev))
            items.append(it)
        out: dict[str, Any] = {
            **envelope(f"{config.JOURNAL_ITEM_SERVICE}/A_JournalEntryItemBasic"),
            "dimension": dimension, "fiscal_year": fy, "periods": [period_from, period_to],
            "currency": currencies, "cost_center_lines_only": cost_center_lines_only,
            "grand_total": str(grand), "distinct_values": len(keys), "shown": len(items), "items": items,
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
        cost center the G/L accounts that drive the change. variance = base −
        compare. Deterministic math in the backend; answers "which cost centers
        increased the most and which accounts explain it".
        """
        def pull(y, pf, pt):
            rows, _ = fetch_actuals(
                ctx, group_by=["CostCenter", "CostCenterName", "GLAccount", "GLAccountName"], currency=currency,
                fiscal_year=y, period_from=pf, period_to=pt, ledger=ledger, company_code=company_code,
                controlling_area=controlling_area, cost_centers=cost_centers, strict=True,
            )
            return rows

        base, comp = pull(base_fiscal_year, base_period_from, base_period_to), pull(compare_fiscal_year, compare_period_from, compare_period_to)
        currencies = ensure_single_currency(base, comp, what="Base and comparison")
        names = {str(r["CostCenter"]): r.get("CostCenterName") for r in comp + base}
        gl_names = {str(r["GLAccount"]): r.get("GLAccountName") for r in comp + base}

        def by_cc_gl(rows):
            acc: dict[str, dict[str, Decimal]] = {}
            for r in rows:
                cell = acc.setdefault(str(r["CostCenter"]), {})
                cell[str(r["GLAccount"])] = cell.get(str(r["GLAccount"]), Decimal(0)) + dec(r["Amount"])
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
        # Driver contributions reconcile exactly to the total change (no residual: every G/L line is included).
        return {**envelope(f"{config.JOURNAL_ITEM_SERVICE}/A_JournalEntryItemBasic"),
                "base": {"fiscal_year": base_fiscal_year, "periods": [base_period_from, base_period_to], "total": str(gb)},
                "compare": {"fiscal_year": compare_fiscal_year, "periods": [compare_period_from, compare_period_to], "total": str(gc)},
                "variance": str(gb - gc), "variance_pct": pct(gb, gc),
                "currency": currencies, "cost_centers": items}

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
        anomaly_z: float = 2.0,
    ) -> dict:
        """Period-by-period series per cost center across a range that may span
        fiscal years (e.g. 2025/007 → 2026/006 = trailing 12 months). Every
        period in the range appears, with 0 where nothing was posted, so
        month-over-month change always compares adjacent periods. A period is
        flagged as an anomaly when it deviates from the cost center's average by
        at least anomaly_threshold_pct AND its z-score is at least anomaly_z
        (needs 4+ periods). Use for "what spiked", "trend", "run-rate" questions.
        """
        fy_to = resolve_year(fiscal_year_to)
        fy_from = int(fiscal_year_from) if fiscal_year_from else fy_to
        chunks = _year_period_range(fy_from, period_from, fy_to, period_to)
        rows: list[dict] = []
        for y, pf, pt in chunks:
            chunk, _ = fetch_actuals(
                ctx, group_by=["CostCenter", "CostCenterName", "FiscalPeriod"],
                currency=currency, fiscal_year=y, period_from=pf, period_to=pt, ledger=ledger,
                company_code=company_code, controlling_area=controlling_area,
                cost_centers=cost_centers, gl_accounts=gl_accounts, strict=True,
            )
            for r in chunk:
                r["_year"] = y
            rows.extend(chunk)
        currencies = ensure_single_currency(rows, what="Trend range")

        all_keys = [f"{y}/{period(p)}" for y, pf, pt in chunks for p in range(pf, pt + 1)]
        series: dict[str, dict[str, Decimal]] = {}
        names: dict[str, Any] = {}
        for r in rows:
            cc = str(r.get("CostCenter"))
            key = f"{r['_year']}/{r.get('FiscalPeriod')}"
            series.setdefault(cc, {})
            series[cc][key] = series[cc].get(key, Decimal(0)) + dec(r["Amount"])
            names[cc] = r.get("CostCenterName")

        warnings = ["Anomaly flags are descriptive (deviation from the cost center's own average), not evidence of an accounting error."]
        if len(all_keys) < MIN_PERIODS_FOR_ANOMALY:
            warnings.append(f"Fewer than {MIN_PERIODS_FOR_ANOMALY} periods in range: no anomaly flags.")
        out_cc = []
        for cc, s in sorted(series.items()):
            vals = [s.get(k, Decimal(0)) for k in all_keys]
            avg = sum(vals, Decimal(0)) / len(vals)
            sd = Decimal(str(statistics.pstdev([float(v) for v in vals]))) if len(vals) > 1 else Decimal(0)
            points, anomalies, prev = [], [], None
            for k, v in zip(all_keys, vals):
                dev = pct(v, avg) if avg else None
                z = round((v - avg) / sd, 2) if sd else None
                pt = {"period": k, "amount": str(v), "mom_change": str(v - prev) if prev is not None else None,
                      "mom_change_pct": pct(v, prev) if prev is not None else None, "vs_avg_pct": dev,
                      "z": str(z) if z is not None else None}
                if (len(vals) >= MIN_PERIODS_FOR_ANOMALY and dev is not None and z is not None
                        and abs(float(dev)) >= anomaly_threshold_pct and abs(float(z)) >= anomaly_z):
                    pt["anomaly"] = True
                    anomalies.append(k)
                points.append(pt)
                prev = v
            out_cc.append({"cost_center": cc, "name": names.get(cc), "total": str(sum(vals, Decimal(0))),
                           "average_per_period": str(round(avg, 2)), "periods": len(vals),
                           "anomalous_periods": anomalies, "series": points})
        return {**envelope(f"{config.JOURNAL_ITEM_SERVICE}/A_JournalEntryItemBasic", warnings=warnings),
                "range": {"from": f"{fy_from}/{period(period_from)}", "to": f"{fy_to}/{period(period_to)}"},
                "currency": currencies, "anomaly_threshold_pct": anomaly_threshold_pct, "anomaly_z": anomaly_z,
                "cost_centers": out_cc}

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
        cost_center_lines_only: bool = True,
        top: int = 5000,
    ) -> dict:
        """Same data as get_cost_center_actuals but returned as CSV text
        (header + rows) for the agent to hand back as a file or paste into Excel.
        Text cells that would start an Excel formula are prefixed with an
        apostrophe. top is capped at S4HC_MAX_INLINE_ROWS (default 5000).
        Returns {row_count, truncated, columns, filename, csv}.
        """
        fy = resolve_year(fiscal_year)
        dims = group_by or DEFAULT_DIMS
        rows, truncated = fetch_actuals(
            ctx, group_by=dims, currency=currency, fiscal_year=fy, period_from=period_from,
            period_to=period_to, ledger=ledger, company_code=company_code, controlling_area=controlling_area,
            top=inline_top(top), cost_center_lines_only=cost_center_lines_only, cost_centers=cost_centers,
            gl_accounts=gl_accounts, profit_centers=profit_centers,
        )
        cols = [c for c in dims if c in config.JOURNAL_ITEM_DIMENSIONS] + ["Amount", "Currency"]
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: (v if k == "Amount" else _csv_safe(v)) for k, v in r.items()})
        return {**envelope(f"{config.JOURNAL_ITEM_SERVICE}/A_JournalEntryItemBasic", truncated=truncated),
                "row_count": len(rows), "truncated": truncated, "columns": cols,
                "filename": f"cost_center_actuals_{fy}_{period(period_from)}-{period(period_to)}.csv", "csv": buf.getvalue()}
