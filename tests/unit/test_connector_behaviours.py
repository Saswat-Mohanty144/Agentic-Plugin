"""Behaviours no test asserted (found by mutation sampling of the coverage batch, 2026-10-29).

M2: OAuth tokens are refreshed refresh_buffer seconds BEFORE expiry, not after.
M3: HTTP 429 is retried with backoff like 502/503/504; a 4xx such as 404 is not retried.
"""

import httpx
import pytest

from hrms_plugin.connectors import auth as auth_module
from hrms_plugin.connectors import dynamic_rest as rest_module
from hrms_plugin.connectors.auth import OAuth2TokenManager
from hrms_plugin.connectors.base import ConnectorConfig
from hrms_plugin.connectors.dynamic_rest import DynamicRESTConnector


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def time(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(auth_module.time, "time", c.time)
    return c


def _oauth(buffer):
    def handler(request):
        return httpx.Response(200, json={"access_token": "tok", "expires_in": 600, "token_type": "Bearer"})

    return OAuth2TokenManager(
        token_url="https://idp.test/token", client_id="c", client_secret="s", refresh_buffer_seconds=buffer,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


@pytest.mark.parametrize("elapsed, expired", [(0, False), (539, False), (540, True), (541, True), (600, True)])
async def test_token_counts_as_expired_inside_the_refresh_buffer(clock, elapsed, expired):
    manager = _oauth(buffer=60)
    assert await manager.get_access_token() == "tok"
    clock.now += elapsed
    assert manager.is_expired() is expired


async def test_token_without_buffer_expires_exactly_at_expiry(clock):
    manager = _oauth(buffer=0)
    await manager.get_access_token()
    clock.now += 599
    assert manager.is_expired() is False
    clock.now += 1
    assert manager.is_expired() is True


def _connector(statuses, sleeps, monkeypatch):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(statuses[min(len(seen), len(statuses)) - 1], json={"ok": True})

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(rest_module.asyncio, "sleep", fake_sleep)
    config = ConnectorConfig(vendor_name="generic", base_url="https://api.test", timeout_seconds=5.0)
    connector = DynamicRESTConnector(config=config, client=httpx.AsyncClient(
        base_url="https://api.test", transport=httpx.MockTransport(handler)))
    return connector, seen


@pytest.mark.parametrize("transient", [429, 502, 503, 504])
async def test_transient_status_is_retried_with_backoff(monkeypatch, transient):
    sleeps = []
    connector, seen = _connector([transient, 200], sleeps, monkeypatch)
    response = await connector._send_request("GET", "/employees")
    assert response.status_code == 200 and len(seen) == 2 and sleeps == [0.5]


@pytest.mark.parametrize("status", [400, 401, 404, 409])
async def test_client_errors_are_not_retried(monkeypatch, status):
    sleeps = []
    connector, seen = _connector([status], sleeps, monkeypatch)
    response = await connector._send_request("GET", "/employees")
    assert response.status_code == status and len(seen) == 1 and sleeps == []
