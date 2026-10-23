from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import secrets as _secrets  # noqa: E402

# Throwaway credentials generated per test run: the fake backend never checks
# them, and nothing credential-like is committed to the repository.
TEST_USER = "test_comm_user"
TEST_PASSWORD = _secrets.token_urlsafe(16)
os.environ.update({
    "S4HC_BASE_URL": "https://my-api.s4hana.cloud.sap",
    "S4HC_USER": TEST_USER, "S4HC_PASSWORD": TEST_PASSWORD,
    "S4HC_PAGE_SIZE": "2",
})

import asyncio  # noqa: E402
import socket  # noqa: E402

import pytest  # noqa: E402
import requests  # noqa: E402

import fake_s4hc  # noqa: E402
from src.s4hc import dependencies  # noqa: E402


LOOPBACK = {"127.0.0.1", "::1", "localhost"}


@pytest.fixture(autouse=True)
def _block_live_network(monkeypatch):
    """Any test that forgets the fake backend fails loudly instead of reaching a
    real SAP tenant. Loopback stays open for the HTTP end-to-end tests."""
    real_connect = socket.socket.connect

    def guarded(self, address):
        host = address[0] if isinstance(address, tuple) else None
        if host is not None and host not in LOOPBACK:
            raise RuntimeError(f"test attempted a live network connection to {address!r}")
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)


@pytest.fixture(autouse=True)
def patch_backend(monkeypatch):
    monkeypatch.setattr(requests.Session, "get", fake_s4hc.fake_get)
    monkeypatch.setattr("src.s4hc.client.time.sleep", lambda s: None)
    fake_s4hc.CALLS.clear()
    fake_s4hc.STATE.update(forbid_costcenter_srv=False, throttle_once=False, bad_auth=False, forbid_gl_srv=False,
                           forbid_plan_srv=False, usd_line=False, next_link=None, forbid_pc_srv=False,
                           forbid_glacct_srv=False, gl_data_forbidden=False, je_down=False, server_page=None,
                           redirect=False)
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
