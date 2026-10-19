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
"""

from __future__ import annotations

import os
import tempfile

# Service roots. Override with the inbound URL path shown in the tenant's
# Communication Arrangement if it differs — never guess by appending _SRV.
COSTCENTER_SERVICE = os.getenv("S4HC_COSTCENTER_SERVICE", "/sap/opu/odata/sap/API_COSTCENTER_SRV")
JOURNAL_ITEM_SERVICE = os.getenv("S4HC_JOURNAL_ITEM_SERVICE", "/sap/opu/odata/sap/API_JOURNALENTRYITEMBASIC_SRV")
PLAN_SERVICE = os.getenv("S4HC_PLAN_SERVICE", "/sap/opu/odata/sap/API_FINPLANNINGENTRYITEM_SRV")
GLLINEITEM_SERVICE = os.getenv("S4HC_GLLINEITEM_SERVICE", "/sap/opu/odata/sap/API_GLACCOUNTLINEITEM")

# Property names verified against SAP Cloud SDK generated clients
# (@sap/cloud-sdk-vdm-cost-center-service 2.1.0,
#  @sap/cloud-sdk-vdm-journal-entry-item-basic-service 2.1.0).
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
    "CostCtrResponsiblePersonName", "CostCenterCurrency", "CostCenterStandardHierArea",
]

# A_JournalEntryItemBasic is an *aggregating* entity: amounts are summed over
# whatever dimensions are in $select. That is what makes server-side
# group-by cheap.
JOURNAL_ITEM_DIMENSIONS = [
    "Ledger", "LedgerName", "LedgerFiscalYear", "ControllingArea", "CompanyCode",
    "CompanyCodeName", "GLAccount", "GLAccountName", "CostCenter", "CostCenterName",
    "ProfitCenter", "ProfitCenterName", "FunctionalArea", "FunctionalAreaName",
    "BusinessArea", "Segment", "PartnerCostCenter", "PartnerProfitCenter",
    "FiscalPeriod", "FiscalYearPeriod", "FiscalYearVariant", "ChartOfAccounts",
    "Plant", "Customer", "ControllingDebitCreditCode", "Project", "WBSElement",
    "OrderID", "CostCtrActivityType", "TransactionCurrency", "CompanyCodeCurrency",
    "GlobalCurrency", "CostSourceUnit",
]
JOURNAL_ITEM_MEASURES = [
    "AmountInTransactionCurrency", "AmountInCompanyCodeCurrency",
    "AmountInGlobalCurrency", "ValuationQuantity",
]
JOURNAL_ITEM_FIELDS = JOURNAL_ITEM_DIMENSIONS + JOURNAL_ITEM_MEASURES

# A_FinPlanningEntryItem (API_FINPLANNINGENTRYITEM_SRV) — verified against
# @sap/cloud-sdk-vdm-financial-planning-entry-item-service 2.1.0. Same shape
# as the actuals entity plus PlanningCategory; also an aggregating entity.
PLAN_ONLY_FIELDS = {"PlanningCategory", "ControllingObjectCurrency", "FinancialTransactionType", "Fund", "GrantID",
                    "BudgetPeriod", "BudgetProcess", "BudgetingType", "FinancialManagementArea", "Division"}
_PLAN_SHARED_WITH_ACTUALS = {
    "Ledger", "LedgerName", "LedgerFiscalYear", "ControllingArea", "CompanyCode", "CompanyCodeName", "GLAccount",
    "GLAccountName", "CostCenter", "CostCenterName", "ProfitCenter", "ProfitCenterName", "FunctionalArea",
    "FunctionalAreaName", "BusinessArea", "Segment", "PartnerCostCenter", "PartnerProfitCenter", "FiscalPeriod",
    "FiscalYearPeriod", "FiscalYearVariant", "ChartOfAccounts", "Plant", "Customer", "ControllingDebitCreditCode",
    "Project", "WBSElement", "OrderID", "CostCtrActivityType", "TransactionCurrency", "CompanyCodeCurrency",
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
    "Segment", "ProfitCenterStandardHierarchy", "ProfitCtrResponsiblePersonName", "ProfitCenterIsBlocked",
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


def get_base_url(override: str | None = None) -> str:
    url = (override or os.getenv("S4HC_BASE_URL") or "").strip().rstrip("/")
    if not url:
        raise RuntimeError(
            "[NOT_CONFIGURED] S4HC_BASE_URL is not set. Expected https://<tenant>-api.s4hana.cloud.sap"
        )
    if not url.startswith("https://") or "@" in url or "?" in url:
        raise RuntimeError("[NOT_CONFIGURED] S4HC_BASE_URL must be a plain https:// origin")
    return url


def get_language() -> str:
    return (os.getenv("S4HC_LANGUAGE") or "EN").strip().upper()


def get_default_ledger() -> str:
    return (os.getenv("S4HC_LEDGER") or "0L").strip()


def get_page_size() -> int:
    return max(1, min(int(os.getenv("S4HC_PAGE_SIZE", "1000")), 5000))


def get_max_rows() -> int:
    return max(1, int(os.getenv("S4HC_MAX_ROWS", "50000")))


def get_max_pages() -> int:
    return max(1, int(os.getenv("S4HC_MAX_PAGES", "100")))


def raw_odata_enabled() -> bool:
    return os.getenv("S4HC_ENABLE_RAW_ODATA", "false").lower() in ("1", "true", "yes")


def resolve_client_cert() -> str | tuple[str, str] | None:
    """requests-compatible `cert` for X.509 communication users, or None."""
    cert_path = os.getenv("S4HC_CLIENT_CERT")
    key_path = os.getenv("S4HC_CLIENT_KEY")
    cert_pem = os.getenv("S4HC_CLIENT_CERT_PEM")
    key_pem = os.getenv("S4HC_CLIENT_KEY_PEM")

    if cert_pem:
        cf = tempfile.NamedTemporaryFile(prefix="s4hc_cert_", suffix=".pem", delete=False)
        cf.write(cert_pem.encode())
        cf.close()
        cert_path = cf.name
        if key_pem:
            kf = tempfile.NamedTemporaryFile(prefix="s4hc_key_", suffix=".pem", delete=False)
            kf.write(key_pem.encode())
            kf.close()
            key_path = kf.name

    if cert_path and key_path:
        return (cert_path, key_path)
    if cert_path:
        return cert_path
    return None


# Dimensions usable for actual-vs-plan alignment (present in both entities).
PLAN_DIMENSIONS = [d for d in JOURNAL_ITEM_DIMENSIONS if d in _PLAN_SHARED_WITH_ACTUALS] + ["PlanningCategory"]
