from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.update({
    "S4HC_BASE_URL": "https://my-api.s4hana.cloud.sap",
    "S4HC_USER": "u", "S4HC_PASSWORD": "p",
    "S4HC_PAGE_SIZE": "2",
})

import asyncio  # noqa: E402

import pytest  # noqa: E402
import requests  # noqa: E402

import fake_s4hc  # noqa: E402
from src.s4hc import dependencies  # noqa: E402


@pytest.fixture(autouse=True)
def patch_backend(monkeypatch):
    monkeypatch.setattr(requests.Session, "get", fake_s4hc.fake_get)
    monkeypatch.setattr("src.s4hc.client.time.sleep", lambda s: None)
    fake_s4hc.CALLS.clear()
    fake_s4hc.STATE.update(forbid_costcenter_srv=False, throttle_once=False, bad_auth=False, forbid_gl_srv=False,
                           forbid_plan_srv=False, usd_line=False, next_link=None)
    dependencies._cached.clear()
    dependencies._master_cache.clear()
    yield


@pytest.fixture
def tool():
    import server

    def _get(name):
        return asyncio.run(server.mcp.get_tool(name)).fn

    return _get


@pytest.fixture
def calls():
    return fake_s4hc.CALLS
