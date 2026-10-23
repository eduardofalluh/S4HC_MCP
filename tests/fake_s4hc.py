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
from urllib.parse import parse_qs, unquote, urlencode, urlparse

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
    {"ControllingArea": "A000", "ProfitCenter": "PC1", "ValidityEndDate": NINES, "ValidityStartDate": "/Date(1577836800000)/", "CompanyCode": "", "Segment": "S1", "ProfitCenterStandardHierarchy": "PH", "ProfitCtrResponsiblePersonName": "C", "ProfitCenterIsBlocked": ""},
    {"ControllingArea": "A000", "ProfitCenter": "PC2", "ValidityEndDate": NINES, "ValidityStartDate": "/Date(1577836800000)/", "CompanyCode": "", "Segment": "S2", "ProfitCenterStandardHierarchy": "PH", "ProfitCtrResponsiblePersonName": "D", "ProfitCenterIsBlocked": "X"},
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

for _line in list(LINE_ITEMS):
    LINE_ITEMS.append({**_line, "CostCenter": "", "CostCenterName": "", "GLAccount": "21100000", "GLAccountName": "Payables",
                       "ProfitCenter": "", "ProfitCenterName": "", "FunctionalArea": "", "FunctionalAreaName": "",
                       **{k: str(-Decimal(_line[k])) for k in ("AmountInCompanyCodeCurrency", "AmountInGlobalCurrency",
                                                                "AmountInTransactionCurrency")}})
for _line in LINE_ITEMS:
    _line.setdefault("Segment", "S1" if _line["CostCenter"] != "CC300" else "S2")
    _line["SegmentName"] = "Segment " + _line["Segment"]

PC_ASSIGNMENTS = [
    {"ControllingArea": "A000", "ProfitCenter": "PC1", "CompanyCode": "1010"},
    {"ControllingArea": "A000", "ProfitCenter": "PC2", "CompanyCode": "1010"},
    {"ControllingArea": "A000", "ProfitCenter": "PC2", "CompanyCode": "1710"},
]
PC_TEXTS = [
    {"Language": lang, "ControllingArea": "A000", "ProfitCenter": pc, "ValidityEndDate": NINES,
     "ProfitCenterName": f"{name} {lang}", "ProfitCenterLongName": f"{name} long {lang}"}
    for pc, name in (("PC1", "Retail"), ("PC2", "Online")) for lang in ("EN", "FR")
]
GL_TEXTS = [
    {"ChartOfAccounts": "YCOA", "GLAccount": gl, "Language": lang, "GLAccountName": f"{name} {lang}",
     "GLAccountLongName": f"{name} long {lang}"}
    for gl, name in (("61000000", "Salaries"), ("65000000", "Travel"), ("11000000", "Bank")) for lang in ("EN", "FR")
]

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
GL_LINE_ITEMS.append({**GL_LINE_ITEMS[0], "Ledger": "2L", "LedgerGLLineItem": "000002"})  # parallel ledger copy
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
         "forbid_plan_srv": False, "usd_line": False, "next_link": None, "forbid_pc_srv": False,
         "forbid_glacct_srv": False, "gl_data_forbidden": False, "je_down": False, "server_page": None,
         "redirect": False}
BASE = "https://my-api.s4hana.cloud.sap"
NON_NEGATIVE = ("FiscalPeriod", "LedgerFiscalYear")  # SAP strips leading zeros on output (display-format NonNegative)


class FakeResp:
    def __init__(self, status, payload=None, text="", headers=None):
        self.status_code, self._payload, self.text, self.headers = status, payload, text, headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError
        return self._payload


def _forbidden(msg="Forbidden"):
    return FakeResp(403, {"error": {"message": {"value": msg}}})


def _parse_eq(filter_: str, field: str) -> list[str]:
    return re.findall(rf"(?<![A-Za-z]){field} eq '([^']*)'", filter_)


def _apply_filter(rows, f: str):
    if not f:
        return rows
    out = []
    for r in rows:
        ok = True
        for field in ("ControllingArea", "CompanyCode", "CostCenter", "ProfitCenter", "CostCenterCategory", "ChartOfAccounts",
                      "Ledger", "LedgerFiscalYear", "FiscalYear", "GLAccount", "PlanningCategory", "FunctionalArea", "Segment",
                      "GLAccountGroup", "CostCenterStandardHierArea", "Language"):
            vals = _parse_eq(f, field)
            if vals and str(r.get(field)) not in vals:
                ok = False
        for field, val in re.findall(r"(?<![A-Za-z])(\w+) ne '([^']*)'", f):
            if str(r.get(field, "")) == val:
                ok = False
        if "ValidityEndDate eq datetime'9999-12-31T00:00:00'" in f and r.get("ValidityEndDate") != NINES:
            ok = False
        if "IsProfitLossAccount eq true" in f and not r.get("IsProfitLossAccount"):
            ok = False
        m = re.search(r"FiscalPeriod ge '(\d+)' and FiscalPeriod le '(\d+)'", f)  # SAP accepts '1' or '001'
        if m and not (m.group(1).zfill(3) <= r.get("FiscalPeriod", "") <= m.group(2).zfill(3)):
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


def _order(rows, orderby: str):
    if not orderby:
        return rows
    field, _, direction = orderby.partition(" ")
    return sorted(rows, key=lambda r: str(r.get(field, "")), reverse=direction.strip().lower() == "desc")


METADATA = {
    "API_COSTCENTER_SRV": ('<EntityType Name="A_CostCenterType"><Key><PropertyRef Name="CostCenter"/></Key>'
                           '<Property Name="CostCenter" Type="Edm.String"/><NavigationProperty Name="to_Text"/></EntityType>'
                           '<EntitySet Name="A_CostCenter" EntityType="NS.A_CostCenterType"/>'
                           '<EntitySet Name="A_CostCenterText" EntityType="NS.A_CostCenterTextType"/>'),
    "API_PROFITCENTER_SRV": ('<EntitySet Name="A_PrftCtrCompanyCodeAssignment" EntityType="NS.A_PrftCtrCompanyCodeAssignmentType"/>'
                             '<EntitySet Name="A_ProfitCenter" EntityType="NS.A_ProfitCenterType"/>'
                             '<EntitySet Name="A_ProfitCenterText" EntityType="NS.A_ProfitCenterTextType"/>'),
    "API_GLACCOUNTINCHARTOFACCOUNTS_SRV": ('<EntitySet Name="A_GLAccountInChartOfAccounts" EntityType="NS.A_GLAccountInChartOfAccountsType"/>'
                                           '<EntitySet Name="A_GLAccountText" EntityType="NS.A_GLAccountTextType"/>'),
    "API_FINPLANNINGENTRYITEM_SRV": '<EntitySet Name="A_FinPlanningEntryItem" EntityType="NS.A_FinPlanningEntryItemType"/>',
    "API_JOURNALENTRYITEMBASIC_SRV": ('<EntityType Name="A_JournalEntryItemBasicType"><Key><PropertyRef Name="ID"/></Key>'
                                      '<Property Name="ID" Type="Edm.String"/><Property Name="CostCenter" Type="Edm.String"/></EntityType>'
                                      '<EntitySet Name="A_JournalEntryItemBasic" EntityType="NS.A_JournalEntryItemBasicType"/>'
                                      '<EntitySet Name="A_CompanyCode" EntityType="NS.A_CompanyCodeType"/>'),
}
FORBID = {"API_COSTCENTER_SRV": "forbid_costcenter_srv", "API_PROFITCENTER_SRV": "forbid_pc_srv",
          "API_GLACCOUNTINCHARTOFACCOUNTS_SRV": "forbid_glacct_srv", "API_FINPLANNINGENTRYITEM_SRV": "forbid_plan_srv",
          "API_GLACCOUNTLINEITEM": "forbid_gl_srv"}


def fake_get(self, url, params=None, headers=None, timeout=None, **kwargs):
    u = urlparse(url)
    q = {k: unquote(v[0]) for k, v in parse_qs(u.query).items()}
    q.update(params or {})
    CALLS.append({"path": u.path, "params": dict(q), "auth": getattr(self, "auth", None)})
    service = u.path.split("/sap/opu/odata/sap/")[-1].split("/")[0]

    if STATE["bad_auth"]:
        return FakeResp(401, {"error": {"message": {"value": "Unauthorized"}}})
    if STATE["redirect"]:
        return FakeResp(302, text="", headers={"Location": "https://login.example.com/"})
    if STATE["throttle_once"]:
        STATE["throttle_once"] = False
        return FakeResp(429, text="slow down", headers={"Retry-After": "0"})
    if STATE["je_down"] and service == "API_JOURNALENTRYITEMBASIC_SRV":
        return FakeResp(503, text="down")
    if service in FORBID and STATE[FORBID[service]]:
        return _forbidden()

    if u.path.endswith("/$metadata"):
        if service == "API_GLACCOUNTLINEITEM":
            return FakeResp(200, text=GL_METADATA)
        return FakeResp(200, text=METADATA.get(service, METADATA["API_JOURNALENTRYITEMBASIC_SRV"]))

    f = q.get("$filter", "")
    select = [s for s in q.get("$select", "").split(",") if s]
    aggregating = False
    if u.path.endswith("API_COSTCENTER_SRV/A_CostCenter"):
        rows = _apply_filter(COST_CENTERS, f)
        if "to_Text" not in q.get("$expand", ""):
            rows = [{k: v for k, v in r.items() if k != "to_Text"} for r in rows]
    elif u.path.endswith("/A_JournalEntryItemBasic"):
        rows, aggregating = _aggregate(_apply_filter(LINE_ITEMS + ([USD_LINE] if STATE["usd_line"] else []), f), select), True
    elif u.path.endswith("/A_FinPlanningEntryItem"):
        rows, aggregating = _aggregate(_apply_filter(PLAN_ITEMS, f), select), True
    elif u.path.endswith("/A_CompanyCode"):
        rows = _apply_filter(COMPANY_CODES, f)
    elif u.path.endswith("/A_PrftCtrCompanyCodeAssignment"):
        rows = _apply_filter(PC_ASSIGNMENTS, f)
    elif u.path.endswith("/A_ProfitCenterText"):
        rows = _apply_filter(PC_TEXTS, f)
    elif u.path.endswith("/A_ProfitCenter"):
        rows = _apply_filter(PROFIT_CENTERS, f)
    elif u.path.endswith("/A_GLAccountText"):
        rows = _apply_filter(GL_TEXTS, f)
    elif u.path.endswith("/A_GLAccountInChartOfAccounts"):
        rows = _apply_filter(GL_ACCOUNTS, f)
    elif u.path.endswith("API_JOURNALENTRYITEMBASIC_SRV/A_CostCenter"):
        rows = [{k: v for k, v in r.items() if k != "to_Text"} for r in _apply_filter(COST_CENTERS, f)]
    elif u.path.endswith("API_GLACCOUNTLINEITEM/GLAccountLineItem"):
        if STATE["gl_data_forbidden"]:
            return _forbidden("You do not have start authorization for R3TR IWSV API_GLACCOUNTLINEITEM")
        rows = _apply_filter(GL_LINE_ITEMS, f)
    elif "Forbidden" in u.path:
        return _forbidden("no")
    else:
        return FakeResp(404, {"error": {"message": {"value": "nope"}}})

    if select and not aggregating:
        keep = set(select) | {"to_Text"} if "to_Text" in q.get("$expand", "") else set(select)
        rows = [{k: v for k, v in r.items() if k in keep} for r in rows]
    rows = _order(rows, q.get("$orderby", ""))
    skip = int(q.get("$skiptoken", q.get("$skip", 0)))
    top = int(q.get("$top", 10**6))
    if STATE["server_page"]:
        top = min(top, STATE["server_page"])
    page = rows[skip: skip + top]
    out_rows = []
    for r in page:
        r = json.loads(json.dumps(r))
        for k in NON_NEGATIVE:
            if isinstance(r.get(k), str) and r[k].isdigit():
                r[k] = str(int(r[k]))
        r["__metadata"] = {"type": "NS.Type", "uri": f"{BASE}{u.path}('x')"}
        r["to_Navigation"] = {"__deferred": {"uri": f"{BASE}{u.path}('x')/to_Navigation"}}
        out_rows.append(r)
    d = {"results": out_rows}
    if STATE["next_link"]:
        d["__next"] = STATE["next_link"]
    elif STATE["server_page"] and skip + top < len(rows):
        rest = {k: v for k, v in q.items() if k not in ("$skip", "$top", "$skiptoken")}
        d["__next"] = f"{BASE}{u.path}?" + urlencode({**rest, "$skiptoken": str(skip + top)})
    return FakeResp(200, {"d": d})
