"""Master data via the value-help entity sets of API_JOURNALENTRYITEMBASIC_SRV."""

from __future__ import annotations

from fastmcp import Context, FastMCP

from .. import config
from ..client import odata_str, validate_fields, validate_id
from ..dependencies import cached, get_client, get_defaults
from ..governance import enforce_company_code, filter_rows_to_scope

CURRENT = "ValidityEndDate eq datetime'9999-12-31T00:00:00'"


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    def list_company_codes(ctx: Context, company_code: str | None = None, controlling_area: str | None = None) -> dict:
        """Company codes of the tenant (A_CompanyCode in API_JOURNALENTRYITEMBASIC_SRV):
        name, currency, chart of accounts, fiscal year variant, controlling area.
        Call this first to learn which chart of accounts / currency / FYV applies.
        Cached for 10 minutes.
        """
        company_code = enforce_company_code(ctx, company_code)
        conds = []
        if company_code:
            conds.append(f"CompanyCode eq {odata_str(validate_id(company_code, 'company_code'))}")
        if controlling_area:
            conds.append(f"ControllingArea eq {odata_str(validate_id(controlling_area, 'controlling_area'))}")
        flt = " and ".join(conds) or None

        def load():
            rows, truncated = get_client(ctx).get_all(
                config.JOURNAL_ITEM_SERVICE, "A_CompanyCode",
                select=config.COMPANYCODE_FIELDS, filter_=flt, orderby="CompanyCode",
            )
            rows = filter_rows_to_scope(ctx, rows)
            return {"count": len(rows), "truncated": truncated, "company_codes": rows}

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
        """Profit center master (A_ProfitCenter). Filters are exact matches.
        Names are not in this entity; actuals rows carry ProfitCenterName.
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
        if company_code:
            conds.append(f"CompanyCode eq {odata_str(validate_id(company_code, 'company_code'))}")
        if profit_center:
            conds.append(f"ProfitCenter eq {odata_str(validate_id(profit_center, 'profit_center'))}")
        if segment:
            conds.append(f"Segment eq {odata_str(validate_id(segment, 'segment'))}")
        if only_current:
            conds.append(CURRENT)
        rows, truncated = get_client(ctx).get_all(
            config.JOURNAL_ITEM_SERVICE, "A_ProfitCenter",
            select=select, filter_=" and ".join(conds) or None, orderby="ProfitCenter",
            top=max(1, min(top, config.get_max_rows())),
        )
        rows = filter_rows_to_scope(ctx, rows)
        return {"count": len(rows), "truncated": truncated, "profit_centers": rows}

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
        type, group, P&L vs balance sheet, functional area. Get the chart of
        accounts from list_company_codes. Use gl_account_from/to for a range
        (e.g. '60000000'..'69999999'). Names are not in this entity; actuals
        rows carry GLAccountName. Cached for 10 minutes.
        """
        select = validate_fields(fields or config.GLACCOUNT_DEFAULT_SELECT, config.GLACCOUNT_FIELDS, "A_GLAccountInChartOfAccounts")
        for must in ("ChartOfAccounts", "GLAccount"):
            if must not in select:
                select.append(must)
        conds = [f"ChartOfAccounts eq {odata_str(validate_id(chart_of_accounts, 'chart_of_accounts'))}"]
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
            return {"count": len(rows), "truncated": truncated, "gl_accounts": rows}

        return cached(ctx, f"gl|{flt}|{','.join(select)}|{top}", load)
