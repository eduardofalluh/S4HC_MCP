"""Generic read-only escape hatch + service discovery."""

from __future__ import annotations

import re

from fastmcp import Context, FastMCP

from .. import config
from ..client import S4HCError, parse_entity_types
from ..dependencies import cached, get_client
from ..governance import get_scope
from .actuals import DATA_NOTICE

SERVICES = {
    "costcenter": config.COSTCENTER_SERVICE,
    "journalentryitem": config.JOURNAL_ITEM_SERVICE,
    "plan": config.PLAN_SERVICE,
    "gllineitem": config.GLLINEITEM_SERVICE,
    "profitcenter": config.PROFITCENTER_SERVICE,
    "glaccount": config.GLACCOUNT_SERVICE,
}
_SAFE_SERVICE = re.compile(r"^/sap/opu/odata/sap/[A-Z0-9_]+(_SRV|_CDS)?$", re.I)
_SAFE_ENTITY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,60}$")
_SAFE_PARAM = re.compile(r"^\$(select|filter|expand|orderby|top|skip|inlinecount)$")


def _drop_person_fields(row: dict) -> dict:
    out = {}
    for k, v in row.items():
        if k in config.PERSON_FIELDS:
            continue
        if isinstance(v, dict) and isinstance(v.get("results"), list):
            v = {**v, "results": [_drop_person_fields(r) for r in v["results"] if isinstance(r, dict)]}
        elif isinstance(v, dict):
            v = _drop_person_fields(v)
        out[k] = v
    return out


def _service_path(service: str) -> str:
    s = service.strip()
    if s.lower() in SERVICES:
        return SERVICES[s.lower()]
    if s.upper().startswith("API_") or s.upper().startswith("YY1_"):
        s = f"/sap/opu/odata/sap/{s.upper()}"
    if not _SAFE_SERVICE.match(s):
        raise S4HCError(f"Unsupported service path {service!r}", "INVALID_SCOPE")
    return s


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    def describe_service(ctx: Context, service: str = "journalentryitem") -> dict:
        """Read $metadata of an S/4HANA Cloud OData service and list its entity
        sets, keys and properties. service = costcenter | journalentryitem |
        plan | gllineitem | profitcenter | glaccount | any released API_*
        service | a custom YY1_*_CDS service exposed via a Custom Communication
        Scenario. Use it to confirm field names on a specific tenant before
        building a custom query. Cached for 10 minutes.
        """
        path = _service_path(service)
        return cached(ctx, f"describe|{path}", lambda: _describe(get_client(ctx).get_metadata(path), path))

    def _describe(xml: str, path: str) -> dict:
        entities = {}
        for name, body in parse_entity_types(xml).items():
            keys = re.findall(r'<PropertyRef Name="([^"]+)"', body)
            props = re.findall(r'<Property Name="([^"]+)" Type="([^"]+)"', body)
            navs = re.findall(r'<NavigationProperty Name="([^"]+)"', body)
            entities[name] = {"keys": keys, "properties": [f"{n} ({t.replace('Edm.', '')})" for n, t in props], "navigation": navs}
        sets = dict(re.findall(r'<EntitySet Name="([^"]+)" EntityType="[^"]*\.([^"]+)"', xml))
        return {"service": path, "entity_sets": sets, "entity_types": entities}

    @mcp.tool()
    def s4hc_odata_read(
        ctx: Context,
        service: str,
        entity_set: str,
        params: dict[str, str] | None = None,
        top: int = 1000,
    ) -> dict:
        """ADMIN / DEVELOPMENT ONLY — disabled unless S4HC_ENABLE_RAW_ODATA=true.
        Generic READ-ONLY OData V2 GET against a released service or a custom
        'YY1_<name>_CDS' view (params: $select/$filter/$expand/$orderby only).
        Refused whenever a data scope (allowed company codes / cost centers) is
        active, because a raw $filter cannot be scope-checked. Use the typed
        tools for analysis.
        """
        if not config.raw_odata_enabled():
            raise S4HCError("s4hc_odata_read is disabled on this deployment (S4HC_ENABLE_RAW_ODATA=false)", "DISABLED")
        if get_scope(ctx).restricted:
            raise S4HCError("Raw OData is not allowed on a scope-restricted connection", "UNAUTHORIZED_SCOPE")
        if not _SAFE_ENTITY.match(entity_set):
            raise S4HCError(f"Invalid entity_set {entity_set!r}", "INVALID_SCOPE")
        params = params or {}
        for k in params:
            if not _SAFE_PARAM.match(k) or k in ("$top", "$skip", "$inlinecount"):
                raise S4HCError(f"Parameter {k!r} not allowed; use top=", "INVALID_SCOPE")
        if not config.person_fields_allowed():
            named = [f for f in config.PERSON_FIELDS if any(f in str(v) for v in params.values())]
            if named:
                raise S4HCError(f"{named} identify a person and are not exposed on this deployment "
                                "(S4HC_EXPOSE_PERSON_FIELDS=false)", "UNSUPPORTED_FIELD")
        rows, truncated = get_client(ctx).get_all(
            _service_path(service), entity_set,
            select=[s for s in params.get("$select", "").split(",") if s] or None,
            filter_=params.get("$filter") or None,
            expand=[s for s in params.get("$expand", "").split(",") if s] or None,
            orderby=params.get("$orderby") or None,
            top=max(1, min(top, config.get_max_rows(), config.get_max_inline_rows())),
        )
        if not config.person_fields_allowed():
            rows = [_drop_person_fields(r) for r in rows]
        return {"count": len(rows), "truncated": truncated, "data_notice": DATA_NOTICE, "rows": rows}
