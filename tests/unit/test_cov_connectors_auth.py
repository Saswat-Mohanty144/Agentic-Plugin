"""Coverage tests for hrms_plugin/connectors/auth.py."""

from __future__ import annotations

import httpx
import pytest

from hrms_plugin.connectors.auth import (
    ApiKeyAuth,
    AuthStrategy,
    BasicAuth,
    OAuth2TokenManager,
)
from hrms_plugin.connectors.base import HRMSAuthError


class ConcreteAuthStrategy(AuthStrategy):
    """Concrete subclass calling super() to test the abstract method."""

    async def get_headers(self):
        return await super().get_headers()


@pytest.mark.asyncio
async def test_auth_strategy_abstract_call():
    """Positive test: Concrete class calling super().get_headers() returns None."""
    strategy = ConcreteAuthStrategy()
    assert await strategy.get_headers() is None


@pytest.mark.asyncio
async def test_api_key_auth_custom_and_default_prefix():
    """Positive and boundary tests for ApiKeyAuth header formatting."""
    # Custom prefix and header
    auth1 = ApiKeyAuth(api_key="secret-key-123", header_name="X-API-Key", prefix="Token")
    assert await auth1.get_headers() == {"X-API-Key": "Token secret-key-123"}

    # Default prefix and header
    auth2 = ApiKeyAuth(api_key="secret-key-456")
    assert await auth2.get_headers() == {"Authorization": "Bearer secret-key-456"}

    # Empty prefix
    auth3 = ApiKeyAuth(api_key="secret-key-789", prefix="")
    assert await auth3.get_headers() == {"Authorization": "secret-key-789"}


@pytest.mark.asyncio
async def test_basic_auth_encoding():
    """Positive test for BasicAuth header generation."""
    auth = BasicAuth(username="test_user", password="test_password")
    headers = await auth.get_headers()
    # base64(test_user:test_password) = dGVzdF91c2VyOnRlc3RfcGFzc3dvcmQ=
    assert headers == {"Authorization": "Basic dGVzdF91c2VyOnRlc3RfcGFzc3dvcmQ="}


@pytest.mark.asyncio
async def test_oauth2_token_manager_client_credentials_flow():
    """Positive test: OAuth2 flow with scope, audience, and refresh_token response."""
    captured_requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(
            status_code=200,
            json={
                "access_token": "act_test_12345",
                "expires_in": 7200,
                "refresh_token": "rft_rotated_999",
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        mgr = OAuth2TokenManager(
            token_url="https://auth.example.com/oauth/token",
            client_id="cid_01",
            client_secret="csec_01",
            scope="read:employees write:leave",
            audience="https://api.example.com",
            grant_type="client_credentials",
            client=client,
        )

        assert mgr.is_expired() is True
        token = await mgr.get_access_token()
        assert token == "act_test_12345"
        assert mgr.is_expired() is False
        assert mgr.refresh_token == "rft_rotated_999"

        # Headers check
        headers = await mgr.get_headers()
        assert headers == {"Authorization": "Bearer act_test_12345"}

        # Token caching: subsequent call does not trigger request
        token2 = await mgr.get_access_token()
        assert token2 == "act_test_12345"
        assert len(captured_requests) == 1

        # Check posted form payload
        req = captured_requests[0]
        assert req.url == "https://auth.example.com/oauth/token"
        form_data = dict(x.split("=") for x in req.content.decode("utf-8").split("&"))
        assert form_data["grant_type"] == "client_credentials"
        assert form_data["client_id"] == "cid_01"
        assert form_data["client_secret"] == "csec_01"
        assert form_data["scope"] == "read%3Aemployees+write%3Aleave"
        assert form_data["audience"] == "https%3A%2F%2Fapi.example.com"


@pytest.mark.asyncio
async def test_oauth2_token_manager_force_refresh_and_refresh_token_grant():
    """Positive test: force_refresh triggers request even if active, sending refresh_token."""
    captured_requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        call_num = len(captured_requests)
        return httpx.Response(
            status_code=200,
            json={
                "access_token": f"act_call_{call_num}",
                "expires_in": 3600,
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        mgr = OAuth2TokenManager(
            token_url="https://auth.example.com/oauth/token",
            client_id="cid_02",
            client_secret="csec_02",
            refresh_token="rf_initial_111",
            grant_type="refresh_token",
            client=client,
        )

        tok1 = await mgr.get_access_token()
        assert tok1 == "act_call_1"

        # Force refresh bypasses expiration check
        tok2 = await mgr.force_refresh()
        assert tok2 == "act_call_2"
        assert len(captured_requests) == 2

        # Verify second request included refresh_token payload
        form_data = dict(x.split("=") for x in captured_requests[1].content.decode("utf-8").split("&"))
        assert form_data["grant_type"] == "refresh_token"
        assert form_data["refresh_token"] == "rf_initial_111"


@pytest.mark.asyncio
async def test_oauth2_token_manager_network_error_raises_auth_error():
    """Negative test: Network communication failure raises HRMSAuthError with status 500."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused by auth gateway", request=request)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        mgr = OAuth2TokenManager(
            token_url="https://unreachable.example.com/token",
            client_id="cid",
            client_secret="csec",
            client=client,
        )

        with pytest.raises(HRMSAuthError) as excinfo:
            await mgr.get_access_token()

        assert excinfo.value.status_code == 500
        assert "Failed to reach OAuth2 token endpoint https://unreachable.example.com/token" in excinfo.value.message


@pytest.mark.asyncio
async def test_oauth2_token_manager_http_error_status_raises_auth_error():
    """Negative test: Server returning non-200 raises HRMSAuthError with server status code."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code=401, text='{"error":"invalid_client"}')

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        mgr = OAuth2TokenManager(
            token_url="https://auth.example.com/token",
            client_id="bad_cid",
            client_secret="bad_csec",
            client=client,
        )

        with pytest.raises(HRMSAuthError) as excinfo:
            await mgr.get_access_token()

        assert excinfo.value.status_code == 401
        assert "OAuth2 token request rejected (401)" in excinfo.value.message
        assert excinfo.value.response_body == '{"error":"invalid_client"}'


@pytest.mark.asyncio
async def test_oauth2_token_manager_missing_access_token_in_200_raises():
    """Boundary test: Server returning 200 without access_token raises HRMSAuthError with status 500."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code=200, json={"token_type": "Bearer"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        mgr = OAuth2TokenManager(
            token_url="https://auth.example.com/token",
            client_id="cid",
            client_secret="csec",
            client=client,
        )

        with pytest.raises(HRMSAuthError) as excinfo:
            await mgr.get_access_token()

        assert excinfo.value.status_code == 500
        assert "missing 'access_token'" in excinfo.value.message


@pytest.mark.asyncio
async def test_oauth2_token_manager_owned_client_cleanup(monkeypatch: pytest.MonkeyPatch):
    """Positive test: OAuth2TokenManager without client creates and closes internal client."""
    closed = False

    async def fake_post(self, url, data=None, headers=None):
        return httpx.Response(status_code=200, json={"access_token": "owned_client_token", "expires_in": 1800})

    orig_aclose = httpx.AsyncClient.aclose

    async def fake_aclose(self):
        nonlocal closed
        closed = True
        await orig_aclose(self)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    monkeypatch.setattr(httpx.AsyncClient, "aclose", fake_aclose)

    mgr = OAuth2TokenManager(
        token_url="https://auth.example.com/token",
        client_id="cid_owned",
        client_secret="csec_owned",
    )
    assert mgr._client is None
    assert mgr._owns_client is True

    token = await mgr.get_access_token()
    assert token == "owned_client_token"
    assert closed is True
