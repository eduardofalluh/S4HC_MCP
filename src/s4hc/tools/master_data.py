"""Master data: company codes, profit centers and G/L accounts.

The value-help entity sets of API_JOURNALENTRYITEMBASIC_SRV are always there.
Names and the profit center ↔ company code assignment come from the optional
API_PROFITCENTER_SRV and API_GLACCOUNTINCHARTOFACCOUNTS_SRV when the
communication user may read them; without them the tools still answer, minus
names, and say where the data came from.
"""

from __future__ import annotations

from typing import Any

from fastmcp import Context, FastMCP

from .. import config
from ..client import S4HCError, odata_str, validate_fields, validate_id
from ..dependencies import cached, get_client, get_defaults
from ..governance import enforce_company_code, filter_rows_to_scope, get_scope
from .actuals import envelope

CURRENT = "ValidityEndDate eq datetime'9999-12-31T00:00:00'"
OPTIONAL_MISSING = ("SAP_FORBIDDEN", "SAP_NOT_FOUND")


def _profit_center_company_codes(ctx: Context | None) -> tuple[dict[str, set[str]], str, bool]:
    """ProfitCenter -> company codes it is assigned to. In S/4HANA the
    CompanyCode field on the profit center master is normally blank; the real
    (n:m) assignment lives in A_PrftCtrCompanyCodeAssignment."""
    def load() -> tuple[dict[str, set[str]], str, bool]:
        client = get_client(ctx)
        out: dict[str, set[str]] = {}
        try:
            rows, truncated = client.get_all(config.PROFITCENTER_SERVICE, "A_PrftCtrCompanyCodeAssignment",
                                     select=["ControllingArea", "ProfitCenter", "CompanyCode"])
            source = "API_PROFITCENTER_SRV/A_PrftCtrCompanyCodeAssignment"
        except S4HCError as exc:
            if exc.code not in OPTIONAL_MISSING:
                raise
            # Fallback: a profit center belongs to the company codes of the cost centers assigned to it.
            rows, truncated = client.get_all(config.JOURNAL_ITEM_SERVICE, "A_CostCenter",
                                     select=["CostCenter", "ProfitCenter", "CompanyCode", "ValidityEndDate"],
                                     filter_=CURRENT)
            source = "derived from cost center assignments (API_PROFITCENTER_SRV not available)"
        for r in rows:
            if r.get("ProfitCenter") and r.get("CompanyCode"):
                out.setdefault(str(r["ProfitCenter"]), set()).add(str(r["CompanyCode"]))
        return out, source, truncated

    return cached(ctx, "pc-company-codes", load)


def _texts(ctx: Context | None, key: str, service: str, entity_set: str, id_field: str, name_fields: list[str],
           filter_: str) -> dict[str, dict[str, Any]] | None:
    """id -> name fields from an optional text entity set, or None if unavailable."""
    def load():
        try:
            rows, _ = get_client(ctx).get_all(service, entity_set, select=[id_field, *name_fields], filter_=filter_)
        except S4HCError as exc:
            if exc.code in OPTIONAL_MISSING:
                return None
            raise
        return {str(r[id_field]): {f: r.get(f) for f in name_fields} for r in rows if r.get(id_field)}

    return cached(ctx, key, load)


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    def list_company_codes(ctx: Context, company_code: str | None = None, controlling_area: str | None = None) -> dict:
        """Company codes of the tenant (A_CompanyCode in API_JOURNALENTRYITEMBASIC_SRV):
        name, currency, chart of accounts, fiscal year variant, controlling area.
        Call this first to learn which chart of accounts / currency / FYV applies.
        Respects the connection's default controlling area. Cached for 10 minutes.
        """
        company_code = enforce_company_code(ctx, company_code)
        conds = []
        if company_code:
            conds.append(f"CompanyCode eq {odata_str(validate_id(company_code, 'company_code'))}")
        ca = controlling_area or get_defaults(ctx).controlling_area
        if ca:
            conds.append(f"ControllingArea eq {odata_str(validate_id(ca, 'controlling_area'))}")
        flt = " and ".join(conds) or None

        def load():
            rows, truncated = get_client(ctx).get_all(
                config.JOURNAL_ITEM_SERVICE, "A_CompanyCode",
                select=config.COMPANYCODE_FIELDS, filter_=flt, orderby="CompanyCode",
            )
            rows = filter_rows_to_scope(ctx, rows, cc_key=None)
            return {**envelope(f"{config.JOURNAL_ITEM_SERVICE}/A_CompanyCode", truncated=truncated),
                    "count": len(rows), "truncated": truncated, "company_codes": rows}

        return cached(ctx, f"cc|{flt}", load)

    @mcp.tool()
    def list_profit_centers(
        ctx: Context,
        controlling_area: str | None = None,
        company_code: str | None = None,
        profit_center: str | None = None,
        segment: str | None = None,
        only_current: bool = True,
        fields: list[str] | None = None,
        top: int = 500,
    ) -> dict:
        """Profit center master with names in the configured language and the
        company codes each profit center is assigned to (company_codes). Filters
        are exact matches; company_code keeps profit centers assigned to that
        company code. Names and assignments come from API_PROFITCENTER_SRV when
        available; otherwise names are missing and the assignment is derived
        from cost centers (source says which).
        """
        d = get_defaults(ctx)
        company_code = enforce_company_code(ctx, company_code)
        select = validate_fields(fields or config.PROFITCENTER_DEFAULT_SELECT, config.PROFITCENTER_FIELDS, "A_ProfitCenter")
        for must in ("ControllingArea", "ProfitCenter", "ValidityEndDate"):
            if must not in select:
                select.append(must)
        conds = []
        ca = controlling_area or d.controlling_area
        if ca:
            conds.append(f"ControllingArea eq {odata_str(validate_id(ca, 'controlling_area'))}")
        if profit_center:
            conds.append(f"ProfitCenter eq {odata_str(validate_id(profit_center, 'profit_center'))}")
        if segment:
            conds.append(f"Segment eq {odata_str(validate_id(segment, 'segment'))}")
        if only_current:
            conds.append(CURRENT)
        flt = " and ".join(conds) or None

        scope = get_scope(ctx)
        wanted_codes = {company_code} if company_code else set(scope.company_codes)
        client = get_client(ctx)
        top = max(1, min(top, config.get_max_rows()))
        try:
            rows, truncated = client.get_all(config.PROFITCENTER_SERVICE, "A_ProfitCenter", select=select,
                                             filter_=flt, orderby="ProfitCenter",
                                             top=None if wanted_codes else top)
            source = "API_PROFITCENTER_SRV"
        except S4HCError as exc:
            if exc.code not in OPTIONAL_MISSING:
                raise
            rows, truncated = client.get_all(config.JOURNAL_ITEM_SERVICE, "A_ProfitCenter", select=select,
                                             filter_=flt, orderby="ProfitCenter",
                                             top=None if wanted_codes else top)
            source = "API_JOURNALENTRYITEMBASIC_SRV (fallback, no names)"

        assignment, assignment_source, assignment_truncated = _profit_center_company_codes(ctx)
        texts = _texts(ctx, f"pc-text|{d.language}", config.PROFITCENTER_SERVICE, "A_ProfitCenterText",
                       "ProfitCenter", ["ProfitCenterName", "ProfitCenterLongName"],
                       f"Language eq {odata_str(d.language)} and {CURRENT}")
        out = []
        for r in rows:
            pc = str(r.get("ProfitCenter"))
            codes = set(assignment.get(pc, set()))
            if r.get("CompanyCode"):
                codes.add(str(r["CompanyCode"]))
            if wanted_codes and not codes & wanted_codes:
                continue  # fails closed: a profit center with no known company code is out of a scoped view
            r["company_codes"] = sorted(codes & wanted_codes if scope.company_codes else codes)
            if texts is not None:
                r.update(texts.get(pc, {"ProfitCenterName": None, "ProfitCenterLongName": None}))
            out.append(r)
        if wanted_codes and len(out) > top:
            out, truncated = out[:top], True
        warnings = [] if texts is not None else ["Profit center names unavailable: API_PROFITCENTER_SRV not readable."]
        if assignment_truncated:
            warnings.append("The profit center ↔ company code assignment exceeded S4HC_MAX_ROWS; company_codes may be "
                            "incomplete and company-code filtering may miss profit centers.")
            truncated = truncated or bool(wanted_codes)
        return {**envelope(source, truncated=truncated, warnings=warnings),
                "count": len(out), "truncated": truncated, "source": source,
                "company_code_source": assignment_source, "language": d.language, "profit_centers": out}

    @mcp.tool()
    def list_gl_accounts(
        ctx: Context,
        chart_of_accounts: str,
        gl_account: str | None = None,
        gl_account_from: str | None = None,
        gl_account_to: str | None = None,
        only_pl: bool = False,
        account_group: str | None = None,
        fields: list[str] | None = None,
        top: int = 2000,
    ) -> dict:
        """G/L accounts in a chart of accounts (A_GLAccountInChartOfAccounts):
        name, type, group, P&L vs balance sheet, functional area. Get the chart
        of accounts from list_company_codes. Use gl_account_from/to for a range
        (e.g. '60000000'..'69999999'). Names (GLAccountName / GLAccountLongName,
        configured language) come from API_GLACCOUNTINCHARTOFACCOUNTS_SRV when
        readable. Cached for 10 minutes.
        """
        d = get_defaults(ctx)
        coa = validate_id(chart_of_accounts, "chart_of_accounts")
        select = validate_fields(fields or config.GLACCOUNT_DEFAULT_SELECT, config.GLACCOUNT_FIELDS, "A_GLAccountInChartOfAccounts")
        for must in ("ChartOfAccounts", "GLAccount"):
            if must not in select:
                select.append(must)
        conds = [f"ChartOfAccounts eq {odata_str(coa)}"]
        if gl_account:
            conds.append(f"GLAccount eq {odata_str(validate_id(gl_account, 'gl_account'))}")
        if gl_account_from:
            conds.append(f"GLAccount ge {odata_str(validate_id(gl_account_from, 'gl_account_from'))}")
        if gl_account_to:
            conds.append(f"GLAccount le {odata_str(validate_id(gl_account_to, 'gl_account_to'))}")
        if only_pl:
            conds.append("IsProfitLossAccount eq true")
        if account_group:
            conds.append(f"GLAccountGroup eq {odata_str(validate_id(account_group, 'account_group'))}")
        flt = " and ".join(conds)
        top = max(1, min(top, config.get_max_rows()))

        def load():
            rows, truncated = get_client(ctx).get_all(
                config.JOURNAL_ITEM_SERVICE, "A_GLAccountInChartOfAccounts",
                select=select, filter_=flt, orderby="GLAccount", top=top,
            )
            texts = _texts(ctx, f"gl-text|{coa}|{d.language}", config.GLACCOUNT_SERVICE, "A_GLAccountText",
                           "GLAccount", ["GLAccountName", "GLAccountLongName"],
                           f"ChartOfAccounts eq {odata_str(coa)} and Language eq {odata_str(d.language)}")
            if texts is not None:
                for r in rows:
                    r.update(texts.get(str(r.get("GLAccount")), {"GLAccountName": None, "GLAccountLongName": None}))
            warnings = [] if texts is not None else ["G/L account names unavailable: API_GLACCOUNTINCHARTOFACCOUNTS_SRV not readable."]
            return {**envelope(f"{config.JOURNAL_ITEM_SERVICE}/A_GLAccountInChartOfAccounts", truncated=truncated,
                               warnings=warnings),
                    "count": len(rows), "truncated": truncated, "language": d.language, "gl_accounts": rows}

        return cached(ctx, f"gl|{flt}|{','.join(select)}|{top}|{d.language}", load)
