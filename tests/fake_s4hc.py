"""Fake S/4HANA Cloud OData backend used by the tests.

Patched into requests.Session.get. Understands $select/$filter/$top/$skip/
$expand for the entity sets the server uses, aggregates
A_JournalEntryItemBasic over $select like the real analytical entity, and can
simulate 403 (no arrangement), 429 (throttle) and 401.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from decimal import Decimal
from urllib.parse import parse_qs, unquote, urlparse

NINES = "/Date(253402214400000)/"  # 9999-12-31

COST_CENTERS = [
    {"ControllingArea": "A000", "CostCenter": "CC100", "ValidityEndDate": NINES, "ValidityStartDate": "/Date(1577836800000)/", "CompanyCode": "1010", "ProfitCenter": "PC1", "CostCenterCategory": "E", "FunctionalArea": "YB10", "CostCtrResponsiblePersonName": "A. Person", "CostCenterCurrency": "CAD", "CostCenterStandardHierArea": "H1",
     "to_Text": {"results": [{"Language": "FR", "CostCenterName": "Ventes", "CostCenterDescription": "Ventes CA"}, {"Language": "EN", "CostCenterName": "Sales", "CostCenterDescription": "Sales CA"}]}},
    {"ControllingArea": "A000", "CostCenter": "CC200", "ValidityEndDate": NINES, "ValidityStartDate": "/Date(1577836800000)/", "CompanyCode": "1010", "ProfitCenter": "PC1", "CostCenterCategory": "E", "FunctionalArea": "YB10", "CostCtrResponsiblePersonName": "", "CostCenterCurrency": "CAD", "CostCenterStandardHierArea": "H1",
     "to_Text": {"results": []}},
    {"ControllingArea": "A000", "CostCenter": "CC300", "ValidityEndDate": NINES, "ValidityStartDate": "/Date(1577836800000)/", "CompanyCode": "1010", "ProfitCenter": "PC2", "CostCenterCategory": "G", "FunctionalArea": "YB20", "CostCtrResponsiblePersonName": "B. Person", "CostCenterCurrency": "CAD", "CostCenterStandardHierArea": "H2",
     "to_Text": {"results": [{"Language": "EN", "CostCenterName": "IT", "CostCenterDescription": "IT CA"}]}},
    {"ControllingArea": "A000", "CostCenter": "CC300", "ValidityEndDate": "/Date(1577750400000)/", "ValidityStartDate": "/Date(946684800000)/", "CompanyCode": "1010", "ProfitCenter": "PC2", "CostCenterCategory": "G", "FunctionalArea": "YB20", "CostCtrResponsiblePersonName": "", "CostCenterCurrency": "CAD", "CostCenterStandardHierArea": "H2",
     "to_Text": {"results": []}},  # old validity slice
]
COMPANY_CODES = [
    {"CompanyCode": "1010", "CompanyCodeName": "Syntax CA", "Currency": "CAD", "ChartOfAccounts": "YCOA", "FiscalYearVariant": "K4", "ControllingArea": "A000", "Country": "CA", "CityName": "Montreal", "Language": "EN", "Company": "", "CountryChartOfAccounts": "", "FinancialManagementArea": ""},
    {"CompanyCode": "1710", "CompanyCodeName": "Syntax US", "Currency": "USD", "ChartOfAccounts": "YCOA", "FiscalYearVariant": "K4", "ControllingArea": "A000", "Country": "US", "CityName": "NY", "Language": "EN", "Company": "", "CountryChartOfAccounts": "", "FinancialManagementArea": ""},
]
PROFIT_CENTERS = [
    {"ControllingArea": "A000", "ProfitCenter": "PC1", "ValidityEndDate": NINES, "ValidityStartDate": "/Date(1577836800000)/", "CompanyCode": "1010", "Segment": "S1", "ProfitCenterStandardHierarchy": "PH", "ProfitCtrResponsiblePersonName": "C", "ProfitCenterIsBlocked": ""},
    {"ControllingArea": "A000", "ProfitCenter": "PC2", "ValidityEndDate": NINES, "ValidityStartDate": "/Date(1577836800000)/", "CompanyCode": "1010", "Segment": "S2", "ProfitCenterStandardHierarchy": "PH", "ProfitCtrResponsiblePersonName": "D", "ProfitCenterIsBlocked": "X"},
]
GL_ACCOUNTS = [
    {"ChartOfAccounts": "YCOA", "GLAccount": "61000000", "GLAccountType": "P", "GLAccountGroup": "PL", "IsBalanceSheetAccount": False, "IsProfitLossAccount": True, "FunctionalArea": "", "AccountIsBlockedForPosting": False},
    {"ChartOfAccounts": "YCOA", "GLAccount": "65000000", "GLAccountType": "P", "GLAccountGroup": "PL", "IsBalanceSheetAccount": False, "IsProfitLossAccount": True, "FunctionalArea": "", "AccountIsBlockedForPosting": False},
    {"ChartOfAccounts": "YCOA", "GLAccount": "11000000", "GLAccountType": "X", "GLAccountGroup": "BS", "IsBalanceSheetAccount": True, "IsProfitLossAccount": False, "FunctionalArea": "", "AccountIsBlockedForPosting": False},
]
# "ACDOCA" line items (un-aggregated); the fake aggregates over $select like SAP.
LINE_ITEMS = []


def _li(year, period, cc, gl, amt, pc="PC1", fa="YB10", ccode="1010"):
    LINE_ITEMS.append({
        "Ledger": "0L", "LedgerName": "Leading", "LedgerFiscalYear": str(year), "FiscalPeriod": f"{period:03d}",
        "FiscalYearPeriod": f"{year}{period:03d}", "ControllingArea": "A000", "CompanyCode": ccode, "CompanyCodeName": "Syntax CA",
        "GLAccount": gl, "GLAccountName": {"61000000": "Salaries", "65000000": "Travel", "62000000": "Software"}[gl],
        "CostCenter": cc, "CostCenterName": {"CC100": "Sales", "CC300": "IT"}[cc], "ProfitCenter": pc, "ProfitCenterName": pc + " name",
        "FunctionalArea": fa, "FunctionalAreaName": fa + " name", "CompanyCodeCurrency": "CAD", "AmountInCompanyCodeCurrency": str(amt),
        "GlobalCurrency": "USD", "AmountInGlobalCurrency": str(Decimal(str(amt)) * Decimal("0.75")),
        "TransactionCurrency": "CAD", "AmountInTransactionCurrency": str(amt),
    })


for p in range(1, 13):
    _li(2026, p, "CC100", "61000000", 1000)        # flat salaries
    _li(2026, p, "CC100", "61000000", 10)          # second line, same cell -> aggregated to 1010
_li(2026, 2, "CC100", "65000000", "250.50")
_li(2026, 6, "CC100", "65000000", "3000.00")       # June travel spike
_li(2026, 1, "CC300", "62000000", "-40.00", pc="PC2", fa="YB20")
_li(2025, 1, "CC100", "61000000", 900)
_li(2025, 12, "CC100", "61000000", 950)

GL_LINE_ITEMS = [
    {"CompanyCode": "1010", "FiscalYear": "2026", "FiscalPeriod": "006", "CostCenter": "CC100", "GLAccount": "65000000", "GLAccountName": "Travel",
     "AmountInCompanyCodeCurrency": "2800.00", "CompanyCodeCurrency": "CAD", "AccountingDocument": "1900000001", "LedgerGLLineItem": "000001",
     "PostingDate": "/Date(1781568000000)/", "DocumentItemText": "Conference Berlin", "Supplier": "SUP01", "Ledger": "0L", "AccountingDocumentType": "KR"},
    {"CompanyCode": "1010", "FiscalYear": "2026", "FiscalPeriod": "006", "CostCenter": "CC100", "GLAccount": "65000000", "GLAccountName": "Travel",
     "AmountInCompanyCodeCurrency": "200.00", "CompanyCodeCurrency": "CAD", "AccountingDocument": "1900000002", "LedgerGLLineItem": "000001",
     "PostingDate": "/Date(1781654400000)/", "DocumentItemText": "Taxi", "Supplier": "", "Ledger": "0L", "AccountingDocumentType": "KR"},
    {"CompanyCode": "1010", "FiscalYear": "2026", "FiscalPeriod": "006", "CostCenter": "CC100", "GLAccount": "61000000", "GLAccountName": "Salaries",
     "AmountInCompanyCodeCurrency": "1010.00", "CompanyCodeCurrency": "CAD", "AccountingDocument": "1900000003", "LedgerGLLineItem": "000001",
     "PostingDate": "/Date(1782086400000)/", "DocumentItemText": "Payroll", "Supplier": "", "Ledger": "0L", "AccountingDocumentType": "SA"},
]
GL_METADATA = ('<EntityType Name="GLAccountLineItemType"><Key><PropertyRef Name="SourceLedger"/></Key>'
               + "".join(f'<Property Name="{p}" Type="Edm.String"/>' for p in GL_LINE_ITEMS[0]) + '</EntityType>'
               '<EntitySet Name="GLAccountLineItem" EntityType="NS.GLAccountLineItemType"/>')

PLAN_ITEMS = []
for _p in range(1, 13):
    for cc, gl, amt in (("CC100", "61000000", 1000), ("CC100", "65000000", 100), ("CC300", "62000000", 50)):
        PLAN_ITEMS.append({"Ledger": "0L", "LedgerFiscalYear": "2026", "FiscalPeriod": f"{_p:03d}", "PlanningCategory": "PLN",
                           "ControllingArea": "A000", "CompanyCode": "1010", "CostCenter": cc,
                           "CostCenterName": {"CC100": "Sales", "CC300": "IT"}[cc], "GLAccount": gl,
                           "GLAccountName": {"61000000": "Salaries", "65000000": "Travel", "62000000": "Software"}[gl],
                           "CompanyCodeCurrency": "CAD", "AmountInCompanyCodeCurrency": str(amt),
                           "GlobalCurrency": "USD", "AmountInGlobalCurrency": str(amt), "TransactionCurrency": "CAD",
                           "AmountInTransactionCurrency": str(amt)})
PLAN_ITEMS.append({**PLAN_ITEMS[0], "PlanningCategory": "FCST", "AmountInCompanyCodeCurrency": "5"})
USD_LINE = {**LINE_ITEMS[0], "CompanyCode": "1710", "CompanyCodeCurrency": "USD", "CostCenter": "CC100"}

CALLS: list[dict] = []
STATE = {"forbid_costcenter_srv": False, "throttle_once": False, "bad_auth": False, "forbid_gl_srv": False,
         "forbid_plan_srv": False, "usd_line": False, "next_link": None}


class FakeResp:
    def __init__(self, status, payload=None, text="", headers=None):
        self.status_code, self._payload, self.text, self.headers = status, payload, text, headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError
        return self._payload


def _parse_eq(filter_: str, field: str) -> list[str]:
    return re.findall(rf"(?<![A-Za-z]){field} eq '([^']*)'", filter_)


def _apply_filter(rows, f: str):
    if not f:
        return rows
    out = []
    for r in rows:
        ok = True
        for field in ("ControllingArea", "CompanyCode", "CostCenter", "ProfitCenter", "CostCenterCategory", "ChartOfAccounts",
                      "Ledger", "LedgerFiscalYear", "FiscalYear", "GLAccount", "PlanningCategory", "FunctionalArea", "Segment", "GLAccountGroup", "CostCenterStandardHierArea"):
            vals = _parse_eq(f, field)
            if vals and str(r.get(field)) not in vals:
                ok = False
        if "ValidityEndDate eq datetime'9999-12-31T00:00:00'" in f and r.get("ValidityEndDate") != NINES:
            ok = False
        if "IsProfitLossAccount eq true" in f and not r.get("IsProfitLossAccount"):
            ok = False
        m = re.search(r"FiscalPeriod ge '(\d+)' and FiscalPeriod le '(\d+)'", f)
        if m and not (m.group(1) <= r.get("FiscalPeriod", "") <= m.group(2)):
            ok = False
        m = re.search(r"GLAccount ge '(\d+)'", f)
        if m and r.get("GLAccount", "") < m.group(1):
            ok = False
        m = re.search(r"GLAccount le '(\d+)'", f)
        if m and r.get("GLAccount", "") > m.group(1):
            ok = False
        if ok:
            out.append(r)
    return out


def _aggregate(rows, select):
    dims = [s for s in select if not s.startswith("Amount") and s != "ValuationQuantity"]
    measures = [s for s in select if s.startswith("Amount")]
    acc = defaultdict(lambda: defaultdict(Decimal))
    for r in rows:
        key = tuple(r.get(d) for d in dims)
        for m in measures:
            acc[key][m] += Decimal(r[m])
    out = []
    for key, ms in acc.items():
        row = dict(zip(dims, key))
        row.update({m: str(v) for m, v in ms.items()})
        out.append(row)
    return out


def fake_get(self, url, params=None, headers=None, timeout=None):
    u = urlparse(url)
    q = {k: unquote(v[0]) for k, v in parse_qs(u.query).items()}
    q.update(params or {})
    CALLS.append({"path": u.path, "params": dict(q), "auth": getattr(self, "auth", None)})

    if STATE["bad_auth"]:
        return FakeResp(401, {"error": {"message": {"value": "Unauthorized"}}})
    if STATE["throttle_once"]:
        STATE["throttle_once"] = False
        return FakeResp(429, text="slow down", headers={"Retry-After": "0"})

    if u.path.endswith("/$metadata"):
        if "API_COSTCENTER_SRV" in u.path and STATE["forbid_costcenter_srv"]:
            return FakeResp(403, {"error": {"message": {"value": "Forbidden"}}})
        if "API_FINPLANNINGENTRYITEM_SRV" in u.path:
            if STATE["forbid_plan_srv"]:
                return FakeResp(403, {"error": {"message": {"value": "Forbidden"}}})
            return FakeResp(200, text='<EntitySet Name="A_FinPlanningEntryItem" EntityType="NS.A_FinPlanningEntryItemType"/>')
        if "API_GLACCOUNTLINEITEM" in u.path:
            if STATE["forbid_gl_srv"]:
                return FakeResp(403, {"error": {"message": {"value": "Forbidden"}}})
            return FakeResp(200, text=GL_METADATA)
        if "API_COSTCENTER_SRV" in u.path:
            xml = ('<EntityType Name="A_CostCenterType"><Key><PropertyRef Name="CostCenter"/></Key>'
                   '<Property Name="CostCenter" Type="Edm.String"/><NavigationProperty Name="to_Text"/></EntityType>'
                   '<EntitySet Name="A_CostCenter" EntityType="NS.A_CostCenterType"/><EntitySet Name="A_CostCenterText" EntityType="NS.A_CostCenterTextType"/>')
        else:
            xml = ('<EntityType Name="A_JournalEntryItemBasicType"><Key><PropertyRef Name="ID"/></Key>'
                   '<Property Name="ID" Type="Edm.String"/><Property Name="CostCenter" Type="Edm.String"/></EntityType>'
                   '<EntitySet Name="A_JournalEntryItemBasic" EntityType="NS.A_JournalEntryItemBasicType"/>'
                   '<EntitySet Name="A_CompanyCode" EntityType="NS.A_CompanyCodeType"/>')
        return FakeResp(200, text=xml)

    f = q.get("$filter", "")
    select = [s for s in q.get("$select", "").split(",") if s]
    if "API_COSTCENTER_SRV/A_CostCenter" in u.path:
        if STATE["forbid_costcenter_srv"]:
            return FakeResp(403, {"error": {"message": {"value": "Forbidden"}}})
        rows = _apply_filter(COST_CENTERS, f)
        if "to_Text" not in q.get("$expand", ""):
            rows = [{k: v for k, v in r.items() if k != "to_Text"} for r in rows]
    elif u.path.endswith("/A_JournalEntryItemBasic"):
        rows = _aggregate(_apply_filter(LINE_ITEMS + ([USD_LINE] if STATE["usd_line"] else []), f), select)
    elif u.path.endswith("/A_FinPlanningEntryItem"):
        if STATE["forbid_plan_srv"]:
            return FakeResp(403, {"error": {"message": {"value": "Forbidden"}}})
        rows = _aggregate(_apply_filter(PLAN_ITEMS, f), select)
    elif u.path.endswith("/A_CompanyCode"):
        rows = _apply_filter(COMPANY_CODES, f)
    elif u.path.endswith("/A_ProfitCenter"):
        rows = _apply_filter(PROFIT_CENTERS, f)
    elif u.path.endswith("/A_GLAccountInChartOfAccounts"):
        rows = _apply_filter(GL_ACCOUNTS, f)
    elif u.path.endswith("API_JOURNALENTRYITEMBASIC_SRV/A_CostCenter"):
        rows = [{k: v for k, v in r.items() if k != "to_Text"} for r in _apply_filter(COST_CENTERS, f)]
    elif u.path.endswith("API_GLACCOUNTLINEITEM/GLAccountLineItem"):
        if STATE["forbid_gl_srv"]:
            return FakeResp(403, {"error": {"message": {"value": "Forbidden"}}})
        rows = _apply_filter(GL_LINE_ITEMS, f)
    elif "Forbidden" in u.path:
        return FakeResp(403, {"error": {"message": {"value": "no"}}})
    else:
        return FakeResp(404, {"error": {"message": {"value": "nope"}}})

    if select and "A_JournalEntryItemBasic" not in u.path:
        keep = set(select) | {"to_Text"} if "to_Text" in q.get("$expand", "") else set(select)
        rows = [{k: v for k, v in r.items() if k in keep} for r in rows]
    top, skip = int(q.get("$top", 1000)), int(q.get("$skip", 0))
    page = rows[skip: skip + top]
    d = {"results": [json.loads(json.dumps(r)) for r in page]}
    if STATE["next_link"]:
        d["__next"] = STATE["next_link"]
    return FakeResp(200, {"d": d})
