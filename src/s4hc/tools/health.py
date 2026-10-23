from __future__ import annotations

import re
import time

from fastmcp import Context, FastMCP

from .. import config
from ..client import S4HCClient, S4HCError
from ..dependencies import connection_info, resolve_connection
from ..governance import get_scope

# (name, service path, entity set read to prove data access, required, what it enables)
SERVICES = (
    ("API_JOURNALENTRYITEMBASIC_SRV", config.JOURNAL_ITEM_SERVICE, "A_CompanyCode", True,
     "actuals, company codes, cost/profit centers, G/L accounts"),
    ("API_COSTCENTER_SRV", config.COSTCENTER_SERVICE, "A_CostCenter", False, "cost center names"),
    ("API_PROFITCENTER_SRV", config.PROFITCENTER_SERVICE, "A_ProfitCenter", False,
     "profit center names and company code assignment"),
    ("API_GLACCOUNTINCHARTOFACCOUNTS_SRV", config.GLACCOUNT_SERVICE, "A_GLAccountText", False, "G/L account names"),
    ("API_GLACCOUNTLINEITEM", config.GLLINEITEM_SERVICE, "GLAccountLineItem", False,
     "get_cost_center_postings (document-level detail)"),
    ("API_FINPLANNINGENTRYITEM_SRV", config.PLAN_SERVICE, "A_FinPlanningEntryItem", False, "get_actual_vs_plan"),
)


HEALTH_BUDGET_SECONDS = 60


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    def s4hc_health(ctx: Context, live: bool = False) -> dict:
        """SAP S/4HANA Cloud (Public Edition) connection diagnostic. Reports the
        resolved API host, auth mode (basic / x509 / per-request header),
        defaults and data scope. With live=true it checks every service the
        tools use: $metadata AND a one-row read, because a communication user
        can be allowed to read a service's metadata yet still lack the
        arrangement to read its data. Uses a short timeout and no retries.
        Never returns data rows.
        """
        info = connection_info(ctx)
        scope = get_scope(ctx)
        scope_info = {"allowed_company_codes": sorted(scope.company_codes) or "unrestricted",
                      "allowed_cost_centers": sorted(scope.cost_centers) or "unrestricted"}
        if not live:
            return {"status": "configured" if info["auth_mode"] != "none" else "unconfigured",
                    "scope": scope_info, **info}
        try:
            conn = resolve_connection(ctx)
            client = S4HCClient(conn.base_url, user=conn.user, password=conn.password, cert=conn.cert,
                                timeout=15, retries=0)
        except S4HCError as exc:
            return {"status": "error", "error": str(exc), "scope": scope_info, **info}
        checks = {}
        budget_end = time.monotonic() + HEALTH_BUDGET_SECONDS
        for name, path, entity_set, required, enables in SERVICES:
            check = {"required": required, "enables": enables}
            if time.monotonic() > budget_end:
                checks[name] = {**check, "ok": False, "error": f"not checked: health budget of {HEALTH_BUDGET_SECONDS}s used up"}
                continue
            try:
                xml = client.get_metadata(path)
                check["entity_sets"] = re.findall(r'EntitySet Name="([^"]+)"', xml)
                client.probe(path, entity_set)
                check["ok"] = True
            except S4HCError as exc:
                check.update(ok=False, error=str(exc))
                if "entity_sets" in check and exc.code == "SAP_FORBIDDEN":
                    check["hint"] = ("Metadata is readable but data access is not: the communication "
                                     "arrangement for this service is missing for this user.")
            checks[name] = check
        required_ok = all(c["ok"] for c in checks.values() if c["required"])
        all_ok = all(c["ok"] for c in checks.values())
        status = "ok" if all_ok else ("ok_with_optional_services_missing" if required_ok else "error")
        return {"status": status, "services": checks, "scope": scope_info, **info}
