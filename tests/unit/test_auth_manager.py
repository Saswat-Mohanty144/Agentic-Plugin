"""Unit tests for host HRMS authentication and autonomous OAuth2 token management."""

import asyncio

import httpx
import pytest

from hrms_plugin.connectors.auth import ApiKeyAuth, BasicAuth, OAuth2TokenManager


@pytest.mark.asyncio
async def test_api_key_and_basic_auth():
    api_auth = ApiKeyAuth("secret_key_123", header_name="X-API-KEY", prefix="")
    hdrs = await api_auth.get_headers()
    assert hdrs == {"X-API-KEY": "secret_key_123"}

    bearer_auth = ApiKeyAuth("jwt_token_abc")
    hdrs = await bearer_auth.get_headers()
    assert hdrs == {"Authorization": "Bearer jwt_token_abc"}

    basic_auth = BasicAuth("admin", "pass123")
    hdrs = await basic_auth.get_headers()
    assert "Basic " in hdrs["Authorization"]


@pytest.mark.asyncio
async def test_oauth2_token_manager_caching_and_refresh():
    call_count = 0

    def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(
            200,
            json={
                "access_token": f"token_v{call_count}",
                "token_type": "Bearer",
                "expires_in": 1,  # 1 second TTL
            },
        )

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        manager = OAuth2TokenManager(
            token_url="https://auth.workday.com/token",
            client_id="client_1",
            client_secret="secret_1",
            refresh_buffer_seconds=0.1,  # Short buffer for testing
            client=client,
        )

        # 1. First fetch
        token1 = await manager.get_access_token()
        assert token1 == "token_v1"
        assert call_count == 1

        # 2. Second fetch should hit in-memory cache without HTTP request
        token2 = await manager.get_access_token()
        assert token2 == "token_v1"
        assert call_count == 1

        # 3. Wait for expiration
        await asyncio.sleep(1.1)

        # 4. Third fetch should auto-refresh
        token3 = await manager.get_access_token()
        assert token3 == "token_v2"
        assert call_count == 2

        # 5. Header injection
        headers = await manager.get_headers()
        assert headers == {"Authorization": "Bearer token_v2"}


@pytest.mark.asyncio
async def test_oauth2_concurrency_lock():
    call_count = 0

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.05)  # Simulate network latency
        return httpx.Response(
            200,
            json={"access_token": "concurrent_token", "expires_in": 3600},
        )

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        manager = OAuth2TokenManager(
            token_url="https://auth.darwinbox.com/token",
            client_id="client_1",
            client_secret="secret_1",
            client=client,
        )

        # Fire 10 concurrent requests at the exact same time
        tokens = await asyncio.gather(*[manager.get_access_token() for _ in range(10)])

        # All 10 callers receive the same token, but only ONE network request was made
        assert all(t == "concurrent_token" for t in tokens)
        assert call_count == 1
