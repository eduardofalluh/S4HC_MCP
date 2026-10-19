from __future__ import annotations

import re

from fastmcp import Context, FastMCP

from .. import config
from ..client import S4HCError
from ..dependencies import connection_info, get_client


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    def s4hc_health(ctx: Context, live: bool = False) -> dict:
        """SAP S/4HANA Cloud (Public Edition) connection diagnostic. Reports the
        resolved API host, auth mode (basic / x509 / per-request header) and
        defaults. With live=true it also calls $metadata on both services to
        prove the communication arrangements are active. Never returns data rows.
        """
        info = connection_info(ctx)
        if not live:
            return {"status": "configured" if info["auth_mode"] != "none" else "unconfigured", **info}
        checks = {}
        try:
            client = get_client(ctx)
        except (S4HCError, RuntimeError) as exc:
            return {"status": "error", "error": str(exc), **info}
        for name, path, required in (
            ("API_JOURNALENTRYITEMBASIC_SRV", config.JOURNAL_ITEM_SERVICE, True),
            ("API_COSTCENTER_SRV", config.COSTCENTER_SERVICE, False),
            ("API_GLACCOUNTLINEITEM", config.GLLINEITEM_SERVICE, False),
            ("API_FINPLANNINGENTRYITEM_SRV", config.PLAN_SERVICE, False),
        ):
            try:
                xml = client.get_metadata(path)
                sets = re.findall(r'EntitySet Name="([^"]+)"', xml)
                checks[name] = {"ok": True, "required": required, "entity_sets": sets}
            except S4HCError as exc:
                checks[name] = {"ok": False, "required": required, "error": str(exc)}
        required_ok = all(c["ok"] for c in checks.values() if c["required"])
        all_ok = all(c["ok"] for c in checks.values())
        status = "ok" if all_ok else ("ok_with_optional_services_missing" if required_ok else "error")
        from ..governance import get_scope
        scope = get_scope(ctx)
        return {"status": status, "services": checks,
                "scope": {"allowed_company_codes": sorted(scope.company_codes) or "unrestricted",
                          "allowed_cost_centers": sorted(scope.cost_centers) or "unrestricted"}, **info}
