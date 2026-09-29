"""Unit tests for outbound resilience: TokenBucketRateLimiter and CircuitBreaker."""

import asyncio
import time

import httpx
import pytest

from hrms_plugin.connectors.base import ConnectorConfig, HRMSConnectorError
from hrms_plugin.connectors.dynamic_rest import DynamicRESTConnector
from hrms_plugin.connectors.resilience import (
    CircuitBreaker,
    CircuitBreakerOpenError,
    CircuitState,
    TokenBucketRateLimiter,
)
from hrms_plugin.schema.canonical import EntityType


@pytest.mark.asyncio
async def test_token_bucket_rate_limiter():
    # 5 requests per second, capacity 2
    limiter = TokenBucketRateLimiter(rate=5.0, capacity=2.0)

    # Initial capacity permits 2 calls immediately
    t0 = time.monotonic()
    w1 = await limiter.acquire(1.0)
    w2 = await limiter.acquire(1.0)
    assert w1 == 0.0
    assert w2 == 0.0

    # Third call must wait for token replenishment (~0.2s)
    w3 = await limiter.acquire(1.0)
    elapsed = time.monotonic() - t0
    assert w3 > 0.0
    assert elapsed >= 0.15


@pytest.mark.asyncio
async def test_circuit_breaker_transitions():
    cb = CircuitBreaker(
        failure_threshold=3,
        recovery_timeout_seconds=0.1,  # Fast timeout for test
        half_open_successes=2,
        vendor_name="test_hrms",
    )

    assert cb.state == CircuitState.CLOSED

    # 1. Record 2 failures (threshold is 3)
    await cb.record_failure(Exception("500 Internal Server Error"))
    await cb.record_failure(Exception("500 Internal Server Error"))
    assert cb.state == CircuitState.CLOSED

    # 2. Third failure trips circuit to OPEN
    await cb.record_failure(Exception("500 Internal Server Error"))
    assert cb.state == CircuitState.OPEN

    # 3. Subsequent request while OPEN should fast-fail without calling host
    with pytest.raises(CircuitBreakerOpenError) as exc_info:
        await cb.before_request()
    assert exc_info.value.vendor == "test_hrms"

    # 4. Wait for recovery timeout
    await asyncio.sleep(0.12)
    assert cb.state == CircuitState.HALF_OPEN

    # 5. Half-open state permits probe
    await cb.before_request()
    await cb.record_success()
    # Needs 2 successes
    assert cb.state == CircuitState.HALF_OPEN
    await cb.record_success()

    # 6. Recovers to CLOSED
    assert cb.state == CircuitState.CLOSED


@pytest.mark.asyncio
async def test_dynamic_connector_with_resilience():
    # Mock transport returning 500
    fail_count = 0

    def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal fail_count
        fail_count += 1
        return httpx.Response(500, json={"error": "Database down"})

    transport = httpx.MockTransport(mock_handler)
    client = httpx.AsyncClient(transport=transport, base_url="https://api.fake-hrms.com")

    cb = CircuitBreaker(failure_threshold=2, recovery_timeout_seconds=1.0)
    limiter = TokenBucketRateLimiter(rate=100.0, capacity=10.0)

    config = ConnectorConfig(base_url="https://api.fake-hrms.com", max_retries=1)
    connector = DynamicRESTConnector(
        config=config,
        circuit_breaker=cb,
        rate_limiter=limiter,
        client=client,
    )

    # 1st request fails
    with pytest.raises(HRMSConnectorError):
        await connector.fetch_entity(EntityType.EMPLOYEE, "101")

    # 2nd request fails -> trips breaker
    with pytest.raises(HRMSConnectorError):
        await connector.fetch_entity(EntityType.EMPLOYEE, "102")

    assert cb.state == CircuitState.OPEN

    # 3rd request fast-fails with CircuitBreakerOpenError without hitting transport
    with pytest.raises(CircuitBreakerOpenError):
        await connector.fetch_entity(EntityType.EMPLOYEE, "103")

    # Client was only called 2 times (not 3)
    assert fail_count == 2
