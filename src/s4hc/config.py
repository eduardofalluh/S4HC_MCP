"""Configuration for the SAP S/4HANA Cloud (Public Edition) MCP server.

Everything comes from environment variables (or per-request headers, see
dependencies.py). Nothing is hard-coded — there is no default tenant.

  S4HC_BASE_URL   https://<tenant>-api.s4hana.cloud.sap   (the *-api* host)
  S4HC_USER       communication user name (from "Maintain Communication Users")
  S4HC_PASSWORD   communication user password
  S4HC_CLIENT_CERT / S4HC_CLIENT_KEY   optional X.509 client cert for mTLS
  S4HC_CLIENT_CERT_PEM / S4HC_CLIENT_KEY_PEM   same, inline PEM (hosted secrets)
  S4HC_LANGUAGE   default text language for master-data names (default EN)
  S4HC_LEDGER     default ledger for actuals (default 0L)
  S4HC_PAGE_SIZE  OData $top per page (default 1000)
  S4HC_MAX_ROWS   safety ceiling per tool call (default 50000)
  S4HC_ALLOWED_HOST_SUFFIXES  hosts an API URL may point at (default SAP's)
  S4HC_EXPOSE_PERSON_FIELDS   allow responsible-person / user-ID fields (default false)
"""

from __future__ import annotations

import atexit
import hashlib
import os
import tempfile
from urllib.parse import urlsplit

from .errors import S4HCError

# Service roots. Override with the inbound URL path shown in the tenant's
# Communication Arrangement if it differs — never guess by appending _SRV.
COSTCENTER_SERVICE = os.getenv("S4HC_COSTCENTER_SERVICE", "/sap/opu/odata/sap/API_COSTCENTER_SRV")
JOURNAL_ITEM_SERVICE = os.getenv("S4HC_JOURNAL_ITEM_SERVICE", "/sap/opu/odata/sap/API_JOURNALENTRYITEMBASIC_SRV")
PLAN_SERVICE = os.getenv("S4HC_PLAN_SERVICE", "/sap/opu/odata/sap/API_FINPLANNINGENTRYITEM_SRV")
GLLINEITEM_SERVICE = os.getenv("S4HC_GLLINEITEM_SERVICE", "/sap/opu/odata/sap/API_GLACCOUNTLINEITEM")
# Optional name/assignment sources; every tool works without them (no names then).
PROFITCENTER_SERVICE = os.getenv("S4HC_PROFITCENTER_SERVICE", "/sap/opu/odata/sap/API_PROFITCENTER_SRV")
GLACCOUNT_SERVICE = os.getenv("S4HC_GLACCOUNT_SERVICE", "/sap/opu/odata/sap/API_GLACCOUNTINCHARTOFACCOUNTS_SRV")

# Fields that name or identify a person (responsible person, creating user).
# They are never selected by default and can only be requested when the
# deployment sets S4HC_EXPOSE_PERSON_FIELDS=true, because tool output goes to
# an LLM provider.
PERSON_FIELDS = frozenset({
    "CostCtrResponsiblePersonName", "CostCtrResponsibleUser", "CostCenterCreatedByUser",
    "ProfitCtrResponsiblePersonName", "ProfitCtrResponsibleUser", "ProfitCenterCreatedByUser",
    "AccountingDocCreatedByUser", "CustomerName",
})

# Property names verified against SAP Cloud SDK generated clients
# (@sap/cloud-sdk-vdm-cost-center-service 2.1.0,
#  @sap/cloud-sdk-vdm-journal-entry-item-basic-service 2.1.0) and against the
# $metadata of a 2026 Public Edition tenant.
COSTCENTER_FIELDS = [
    "ControllingArea", "CostCenter", "ValidityEndDate", "ValidityStartDate",
    "CompanyCode", "BusinessArea", "CostCtrResponsiblePersonName",
    "CostCtrResponsibleUser", "CostCenterCurrency", "ProfitCenter", "Department",
    "CostingSheet", "FunctionalArea", "Country", "Region", "CityName",
    "CostCenterStandardHierArea", "CostCenterCategory",
    "IsBlkdForPrimaryCostsPosting", "IsBlkdForSecondaryCostsPosting",
    "IsBlockedForRevenuePosting", "IsBlockedForCommitmentPosting",
    "IsBlockedForPlanPrimaryCosts", "IsBlockedForPlanSecondaryCosts",
    "IsBlockedForPlanRevenues", "ConsumptionQtyIsRecorded", "Language",
    "CostCenterCreatedByUser", "CostCenterCreationDate",
]
COSTCENTER_DEFAULT_SELECT = [
    "ControllingArea", "CostCenter", "ValidityStartDate", "ValidityEndDate",
    "CompanyCode", "ProfitCenter", "CostCenterCategory", "FunctionalArea",
    "CostCenterCurrency", "CostCenterStandardHierArea",
]

# A_JournalEntryItemBasic is an *aggregating* entity: amounts are summed over
# whatever dimensions are in $select. That is what makes server-side
# group-by cheap.
JOURNAL_ITEM_DIMENSIONS = [
    "Ledger", "LedgerName", "LedgerFiscalYear", "ControllingArea", "ControllingAreaName", "CompanyCode",
    "CompanyCodeName", "GLAccount", "GLAccountName", "CostCenter", "CostCenterName",
    "ProfitCenter", "ProfitCenterName", "FunctionalArea", "FunctionalAreaName",
    "BusinessArea", "BusinessAreaName", "Segment", "SegmentName",
    "PartnerCostCenter", "PartnerCostCenterName", "PartnerProfitCenter", "PartnerProfitCenterName",
    "PartnerFunctionalArea", "PartnerFunctionalAreaName", "PartnerCompany", "PartnerCompanyName",
    "FiscalPeriod", "FiscalYearPeriod", "FiscalYearVariant", "ChartOfAccounts",
    "Plant", "PlantName", "Customer", "ControllingDebitCreditCode", "Project", "ProjectDescription",
    "WBSElement", "WBSElementDescription", "OrderID", "CostCtrActivityType", "CostCtrActivityTypeName",
    "TransactionCurrency", "CompanyCodeCurrency", "GlobalCurrency", "CostSourceUnit",
]
JOURNAL_ITEM_MEASURES = [
    "AmountInTransactionCurrency", "AmountInCompanyCodeCurrency",
    "AmountInGlobalCurrency", "ValuationQuantity",
]
JOURNAL_ITEM_FIELDS = JOURNAL_ITEM_DIMENSIONS + JOURNAL_ITEM_MEASURES

# Text field that names each dimension's value, where the entity carries one.
NAME_FOR = {
    "CostCenter": "CostCenterName", "ProfitCenter": "ProfitCenterName", "GLAccount": "GLAccountName",
    "FunctionalArea": "FunctionalAreaName", "CompanyCode": "CompanyCodeName", "BusinessArea": "BusinessAreaName",
    "Segment": "SegmentName", "PartnerCostCenter": "PartnerCostCenterName",
    "PartnerProfitCenter": "PartnerProfitCenterName", "PartnerFunctionalArea": "PartnerFunctionalAreaName",
    "PartnerCompany": "PartnerCompanyName", "Plant": "PlantName", "CostCtrActivityType": "CostCtrActivityTypeName",
    "Project": "ProjectDescription", "WBSElement": "WBSElementDescription", "Ledger": "LedgerName",
    "ControllingArea": "ControllingAreaName",
}

# A_FinPlanningEntryItem (API_FINPLANNINGENTRYITEM_SRV) — verified against
# @sap/cloud-sdk-vdm-financial-planning-entry-item-service 2.1.0 and tenant
# $metadata. Same shape as the actuals entity plus PlanningCategory; also an
# aggregating entity.
PLAN_ONLY_FIELDS = {"PlanningCategory", "ControllingObjectCurrency", "FinancialTransactionType", "Fund", "GrantID",
                    "BudgetPeriod", "BudgetProcess", "BudgetingType", "FinancialManagementArea", "Division"}
_PLAN_SHARED_WITH_ACTUALS = {
    "Ledger", "LedgerName", "LedgerFiscalYear", "ControllingArea", "ControllingAreaName", "CompanyCode",
    "CompanyCodeName", "GLAccount", "GLAccountName", "CostCenter", "CostCenterName", "ProfitCenter",
    "ProfitCenterName", "FunctionalArea", "FunctionalAreaName", "BusinessArea", "BusinessAreaName", "Segment",
    "SegmentName", "PartnerCostCenter", "PartnerCostCenterName", "PartnerProfitCenter", "PartnerProfitCenterName",
    "PartnerFunctionalArea", "PartnerFunctionalAreaName", "PartnerCompany", "PartnerCompanyName", "FiscalPeriod",
    "FiscalYearPeriod", "FiscalYearVariant", "ChartOfAccounts", "Plant", "PlantName", "Customer",
    "ControllingDebitCreditCode", "Project", "ProjectDescription", "WBSElement", "WBSElementDescription", "OrderID",
    "CostCtrActivityType", "CostCtrActivityTypeName", "TransactionCurrency", "CompanyCodeCurrency",
    "GlobalCurrency", "CostSourceUnit",
}

# Value-help entity sets shipped inside API_JOURNALENTRYITEMBASIC_SRV. One
# communication arrangement therefore covers master data *and* actuals.
COMPANYCODE_FIELDS = [
    "CompanyCode", "CompanyCodeName", "CityName", "Country", "Currency", "Language",
    "ChartOfAccounts", "FiscalYearVariant", "Company", "ControllingArea",
    "CountryChartOfAccounts", "FinancialManagementArea",
]
PROFITCENTER_FIELDS = [
    "ControllingArea", "ProfitCenter", "ValidityEndDate", "ValidityStartDate",
    "ProfitCtrResponsiblePersonName", "ProfitCtrResponsibleUser", "CompanyCode",
    "Department", "ProfitCenterStandardHierarchy", "Segment", "ProfitCenterIsBlocked",
    "Country", "Region", "CityName", "Language", "ProfitCenterCreationDate",
]
PROFITCENTER_DEFAULT_SELECT = [
    "ControllingArea", "ProfitCenter", "ValidityStartDate", "ValidityEndDate", "CompanyCode",
    "Segment", "ProfitCenterStandardHierarchy", "ProfitCenterIsBlocked",
]
GLACCOUNT_FIELDS = [
    "ChartOfAccounts", "GLAccount", "GLAccountExternal", "GLAccountType", "GLAccountGroup",
    "IsBalanceSheetAccount", "IsProfitLossAccount", "ProfitLossAccountType", "CorporateGroupAccount",
    "FunctionalArea", "PartnerCompany", "AccountIsBlockedForPosting", "AccountIsBlockedForPlanning",
    "AccountIsMarkedForDeletion", "CreationDate",
]
GLACCOUNT_DEFAULT_SELECT = [
    "ChartOfAccounts", "GLAccount", "GLAccountType", "GLAccountGroup", "IsBalanceSheetAccount",
    "IsProfitLossAccount", "FunctionalArea", "AccountIsBlockedForPosting",
]

DEFAULT_HOST_SUFFIXES = (".s4hana.cloud.sap", ".s4hana.ondemand.com")


def allowed_host_suffixes() -> tuple[str, ...]:
    raw = os.getenv("S4HC_ALLOWED_HOST_SUFFIXES")
    if not raw:
        return DEFAULT_HOST_SUFFIXES
    return tuple(s.strip().lower() for s in raw.split(",") if s.strip())


def get_base_url(override: str | None = None) -> str:
    """The API origin to call: the header value when given, else S4HC_BASE_URL.

    Only a plain https origin on an allowed host suffix is accepted, so neither a
    caller-supplied header nor a typo can point the server (and its credentials)
    at an arbitrary host."""
    url = (override or os.getenv("S4HC_BASE_URL") or "").strip().rstrip("/")
    if not url:
        raise S4HCError("S4HC_BASE_URL is not set. Expected https://<tenant>-api.s4hana.cloud.sap", "NOT_CONFIGURED")
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if (parts.scheme != "https" or not host or parts.username or parts.password or parts.query
            or parts.fragment or parts.path not in ("", "/")):
        raise S4HCError("The S/4HANA Cloud API URL must be a plain https:// origin with no path, user or query",
                        "NOT_CONFIGURED")
    if not any(host == s.lstrip(".") or host.endswith(s if s.startswith(".") else "." + s)
               for s in allowed_host_suffixes()):
        raise S4HCError(f"Host {host!r} is not an allowed S/4HANA Cloud API host "
                        "(set S4HC_ALLOWED_HOST_SUFFIXES to allow it)", "NOT_CONFIGURED")
    return f"https://{host}" + (f":{parts.port}" if parts.port else "")


def get_language() -> str:
    return (os.getenv("S4HC_LANGUAGE") or "EN").strip().upper()


def get_default_ledger() -> str:
    return (os.getenv("S4HC_LEDGER") or "0L").strip()


def get_page_size() -> int:
    return max(1, min(int(os.getenv("S4HC_PAGE_SIZE", "1000")), 5000))


def get_max_rows() -> int:
    return max(1, int(os.getenv("S4HC_MAX_ROWS", "50000")))


def get_max_inline_rows() -> int:
    """Ceiling on raw rows a tool returns to the model in one response."""
    return max(1, int(os.getenv("S4HC_MAX_INLINE_ROWS", "5000")))


def get_max_pages() -> int:
    return max(1, int(os.getenv("S4HC_MAX_PAGES", "100")))


def get_request_deadline() -> float:
    """Wall-clock budget in seconds for one paged read, retries included."""
    return max(5.0, float(os.getenv("S4HC_REQUEST_DEADLINE_SECONDS", "120")))


def raw_odata_enabled() -> bool:
    return os.getenv("S4HC_ENABLE_RAW_ODATA", "false").lower() in ("1", "true", "yes")


def person_fields_allowed() -> bool:
    return os.getenv("S4HC_EXPOSE_PERSON_FIELDS", "false").lower() in ("1", "true", "yes")


_cert_cache: dict[str, str | tuple[str, str] | None] = {}
_temp_files: list[str] = []


def _cleanup_temp_files() -> None:
    for path in _temp_files:
        try:
            os.unlink(path)
        except OSError:
            pass


atexit.register(_cleanup_temp_files)


def _write_private(prefix: str, pem: str) -> str:
    fd, path = tempfile.mkstemp(prefix=prefix, suffix=".pem")  # created 0600
    with os.fdopen(fd, "w") as fh:
        fh.write(pem)
    _temp_files.append(path)
    return path


def resolve_client_cert() -> str | tuple[str, str] | None:
    """requests-compatible `cert` for X.509 communication users, or None.

    Inline PEMs are written to one owner-only temp file per distinct value and
    reused for the life of the process (removed at exit), not one per call."""
    cert_path = os.getenv("S4HC_CLIENT_CERT")
    key_path = os.getenv("S4HC_CLIENT_KEY")
    cert_pem = os.getenv("S4HC_CLIENT_CERT_PEM")
    key_pem = os.getenv("S4HC_CLIENT_KEY_PEM")
    ident = hashlib.sha256("\0".join(v or "" for v in (cert_path, key_path, cert_pem, key_pem)).encode()).hexdigest()
    if ident in _cert_cache:
        return _cert_cache[ident]

    if cert_pem:
        cert_path = _write_private("s4hc_cert_", cert_pem)
        if key_pem:
            key_path = _write_private("s4hc_key_", key_pem)

    resolved: str | tuple[str, str] | None
    if cert_path and key_path:
        resolved = (cert_path, key_path)
    elif cert_path:
        resolved = cert_path
    else:
        resolved = None
    _cert_cache[ident] = resolved
    return resolved


# Dimensions usable for actual-vs-plan alignment (present in both entities).
PLAN_DIMENSIONS = [d for d in JOURNAL_ITEM_DIMENSIONS if d in _PLAN_SHARED_WITH_ACTUALS] + ["PlanningCategory"]
