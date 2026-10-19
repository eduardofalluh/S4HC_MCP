from __future__ import annotations

from typing import Any

from fastmcp import Context, FastMCP

from .. import config
from ..client import S4HCError, odata_str, validate_fields, validate_id
from ..dependencies import get_client, get_defaults
from ..governance import allowed_company_codes_for_filter, enforce_company_code, enforce_cost_centers, filter_rows_to_scope

CURRENT = "ValidityEndDate eq datetime'9999-12-31T00:00:00'"


def _pick_text(row: dict[str, Any], language: str) -> dict[str, Any]:
    texts = (row.pop("to_Text", None) or {}).get("results", []) if isinstance(row.get("to_Text"), dict) else []
    chosen = None
    for t in texts:
        if str(t.get("Language", "")).upper() == language:
            chosen = t
            break
    if chosen is None and texts:
        chosen = texts[0]
    row["CostCenterName"] = (chosen or {}).get("CostCenterName")
    row["CostCenterDescription"] = (chosen or {}).get("CostCenterDescription")
    return row


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    def list_cost_centers(
        ctx: Context,
        controlling_area: str | None = None,
        company_code: str | None = None,
        cost_center: str | None = None,
        profit_center: str | None = None,
        category: str | None = None,
        hierarchy_area: str | None = None,
        only_current: bool = True,
        fields: list[str] | None = None,
        top: int = 500,
    ) -> dict:
        """List cost center master data (A_CostCenter) with names in the
        configured language. Filters are exact matches; cost_center accepts a
        single ID; hierarchy_area = standard hierarchy node. only_current keeps
        the validity slice ending 9999-12-31. Use fields to add any A_CostCenter
        property (CompanyCode, ProfitCenter, CostCenterCategory, FunctionalArea,
        Department, CostCtrResponsiblePersonName, CostCenterStandardHierArea, ...).
        Primary source API_COSTCENTER_SRV (SAP_COM_0087); if that arrangement is
        missing it falls back to the same entity inside
        API_JOURNALENTRYITEMBASIC_SRV (then without names — source is reported).
        Returns {count, truncated, source, cost_centers:[...]}.
        """
        d = get_defaults(ctx)
        client = get_client(ctx)
        select = validate_fields(fields or config.COSTCENTER_DEFAULT_SELECT, config.COSTCENTER_FIELDS, "A_CostCenter")
        for must in ("ControllingArea", "CostCenter", "ValidityEndDate"):
            if must not in select:
                select.append(must)

        company_code = enforce_company_code(ctx, company_code)
        scoped_ccs = enforce_cost_centers(ctx, [cost_center] if cost_center else None)
        conds: list[str] = []
        ca = controlling_area or d.controlling_area
        if ca:
            conds.append(f"ControllingArea eq {odata_str(validate_id(ca, 'controlling_area'))}")
        if company_code:
            conds.append(f"CompanyCode eq {odata_str(validate_id(company_code, 'company_code'))}")
        elif allowed_company_codes_for_filter(ctx, company_code):
            conds.append("(" + " or ".join(f"CompanyCode eq {odata_str(c)}" for c in allowed_company_codes_for_filter(ctx, company_code)) + ")")
        if scoped_ccs:
            conds.append("(" + " or ".join(f"CostCenter eq {odata_str(validate_id(c, 'cost_center'))}" for c in scoped_ccs) + ")")
        if profit_center:
            conds.append(f"ProfitCenter eq {odata_str(validate_id(profit_center, 'profit_center'))}")
        if category:
            conds.append(f"CostCenterCategory eq {odata_str(validate_id(category, 'category'))}")
        if hierarchy_area:
            conds.append(f"CostCenterStandardHierArea eq {odata_str(validate_id(hierarchy_area, 'hierarchy_area'))}")
        if only_current:
            conds.append(CURRENT)
        flt = " and ".join(conds) or None
        top = max(1, min(top, config.get_max_rows()))

        try:
            rows, truncated = client.get_all(
                config.COSTCENTER_SERVICE, "A_CostCenter",
                select=select + ["to_Text"], filter_=flt, expand=["to_Text"], orderby="CostCenter", top=top,
            )
            source = "API_COSTCENTER_SRV"
            out = [_pick_text(r, d.language) for r in rows]
        except S4HCError as exc:
            if exc.code not in ("SAP_FORBIDDEN", "SAP_NOT_FOUND"):
                raise
            rows, truncated = client.get_all(
                config.JOURNAL_ITEM_SERVICE, "A_CostCenter",
                select=select, filter_=flt, orderby="CostCenter", top=top,
            )
            source = "API_JOURNALENTRYITEMBASIC_SRV (fallback, no names — enable SAP_COM_0087 for names)"
            out = rows
        out = filter_rows_to_scope(ctx, out)
        return {"count": len(out), "truncated": truncated, "source": source, "language": d.language, "cost_centers": out}
