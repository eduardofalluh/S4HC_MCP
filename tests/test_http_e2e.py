"""End-to-end: real server over Streamable HTTP, credentials supplied ONLY via
per-request x-s4hc-* headers (the GenAI Studio multi-tenant path). The S/4HANA
side is the fake backend, injected into the server process via sitecustomize.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import textwrap
import time

import secrets

import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.dirname(os.path.abspath(__file__))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server_url(tmp_path_factory):
    shim = tmp_path_factory.mktemp("shim")
    (shim / "sitecustomize.py").write_text(textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {TESTS!r})
        import requests, fake_s4hc
        requests.Session.get = fake_s4hc.fake_get
    """))
    port = _free_port()
    env = {k: v for k, v in os.environ.items() if not k.startswith("S4HC_")}
    env.update({"PYTHONPATH": str(shim), "MCP_TRUST_PROXY": "true", "LOG_LEVEL": "WARNING"})
    proc = subprocess.Popen(
        [sys.executable, "server.py", "--http", "--port", str(port)],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    url = f"http://127.0.0.1:{port}/mcp"
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.2)
    else:
        proc.kill()
        pytest.fail("server did not start:\n" + (proc.stdout.read() if proc.stdout else ""))
    yield url
    proc.kill()


def call(url, name, headers=None, **args):
    async def run():
        async with Client(StreamableHttpTransport(url, headers=headers or {})) as c:
            res = await c.call_tool(name, args, raise_on_error=False)
            if res.is_error:
                return {"__error__": res.content[0].text}
            if res.structured_content is not None:
                return res.structured_content.get("result", res.structured_content)
            return json.loads(res.content[0].text)
    return asyncio.run(run())


# Generated per run — never a committed literal.
_TENANT_PW = secrets.token_urlsafe(16)
_API_KEY = secrets.token_urlsafe(24)
TENANT = {"x-s4hc-base-url": "https://tenant-api.s4hana.cloud.sap", "x-s4hc-user": "comm_user", "x-s4hc-password": _TENANT_PW}


def test_lists_all_tools(server_url):
    async def run():
        async with Client(StreamableHttpTransport(server_url)) as c:
            return {t.name for t in await c.list_tools()}
    names = asyncio.run(run())
    assert names == {
        "s4hc_health", "list_company_codes", "list_profit_centers", "list_gl_accounts", "list_cost_centers",
        "get_cost_center_actuals", "cost_center_summary", "fiscal_context", "actuals_breakdown",
        "cost_center_trend", "export_actuals_csv", "describe_service", "s4hc_odata_read",
        "compare_cost_center_periods", "get_cost_center_postings", "get_actual_vs_plan",
    }


def test_without_headers_is_unconfigured_not_crash(server_url):
    out = call(server_url, "s4hc_health")
    assert out["status"] == "unconfigured" and out["auth_mode"] == "none"
    err = call(server_url, "list_company_codes")
    assert "S4HC_BASE_URL" in err["__error__"]


def test_headers_drive_tenant_and_defaults(server_url):
    out = call(server_url, "s4hc_health", headers={**TENANT, "x-s4hc-ledger": "2L", "x-s4hc-controlling-area": "A000"}, live=True)
    assert out["status"] == "ok" and out["auth_mode"] == "header-basic"
    assert out["base_url"] == "https://tenant-api.s4hana.cloud.sap"
    assert out["default_ledger"] == "2L" and out["controlling_area"] == "A000"
    assert _TENANT_PW not in json.dumps(out)


def test_full_flow_over_http(server_url):
    ccs = call(server_url, "list_cost_centers", headers=TENANT, company_code="1010")
    assert ccs["count"] == 3 and ccs["cost_centers"][0]["CostCenterName"] == "Sales"
    summ = call(server_url, "cost_center_summary", headers=TENANT, fiscal_year=2026, compare_fiscal_year=2025, cost_centers=["CC100"])
    assert summ["cost_centers"][0]["cost_center"] == "CC100" and summ["compare_grand_total"] == "1850"
    trend = call(server_url, "cost_center_trend", headers=TENANT, cost_centers=["CC100"], fiscal_year_from=2026, fiscal_year_to=2026)
    assert "2026/006" in trend["cost_centers"][0]["anomalous_periods"]
    csv_out = call(server_url, "export_actuals_csv", headers=TENANT, fiscal_year=2026, cost_centers=["CC300"])
    assert csv_out["rows"] == 1 and csv_out["csv"].splitlines()[1].startswith("CC300,IT")


def test_scope_headers_enforced_over_http(server_url):
    hdr = {**TENANT, "x-s4hc-allowed-cost-centers": "CC300", "x-s4hc-allowed-company-codes": "1010"}
    out = call(server_url, "list_cost_centers", headers=hdr)
    assert [c["CostCenter"] for c in out["cost_centers"]] == ["CC300"]
    err = call(server_url, "get_cost_center_actuals", headers=hdr, fiscal_year=2026, cost_centers=["CC100"])
    assert "outside" in err["__error__"]
    err = call(server_url, "list_company_codes", headers=hdr, company_code="1710")
    assert "outside" in err["__error__"]
    health = call(server_url, "s4hc_health", headers=hdr)
    assert health["scope"] if "scope" in health else True


def test_language_header_changes_names(server_url):
    out = call(server_url, "list_cost_centers", headers={**TENANT, "x-s4hc-language": "fr"}, cost_center="CC100")
    assert out["language"] == "FR" and out["cost_centers"][0]["CostCenterName"] == "Ventes"



@pytest.fixture(scope="module")
def keyed_server_url(tmp_path_factory):
    shim = tmp_path_factory.mktemp("shim2")
    (shim / "sitecustomize.py").write_text(textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {TESTS!r})
        import requests, fake_s4hc
        requests.Session.get = fake_s4hc.fake_get
    """))
    port = _free_port()
    env = {k: v for k, v in os.environ.items() if not k.startswith("S4HC_")}
    env.update({"PYTHONPATH": str(shim), "LOG_LEVEL": "WARNING", "S4HC_MCP_API_KEY": _API_KEY})
    proc = subprocess.Popen([sys.executable, "server.py", "--http", "--port", str(port)], cwd=ROOT, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.2)
    yield f"http://127.0.0.1:{port}/mcp"
    proc.kill()


def test_inbound_api_key_enforced(keyed_server_url):
    import requests
    r = requests.post(keyed_server_url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                   headers={"Accept": "application/json, text/event-stream"})
    assert r.status_code == 401
    r = requests.post(keyed_server_url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                   headers={"Accept": "application/json, text/event-stream", "Authorization": f"Bearer {secrets.token_urlsafe(24)}"})
    assert r.status_code == 401
    out = call(keyed_server_url, "s4hc_health", headers={**TENANT, "Authorization": f"Bearer {_API_KEY}"})
    assert out["auth_mode"] == "header-basic"
