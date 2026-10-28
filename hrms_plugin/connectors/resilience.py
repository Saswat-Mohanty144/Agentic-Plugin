"""Outbound Resilience Suite: Token-Bucket Rate Limiter and Circuit Breaker."""

from __future__ import annotations

import asyncio
import logging
import time
from enum import Enum
from typing import Optional

from hrms_plugin.connectors.base import HRMSConnectorError

logger = logging.getLogger(__name__)


class CircuitState(str, Enum):
    CLOSED = "CLOSED"      # Normal operation: traffic flows through
    OPEN = "OPEN"          # Host failing: fast-fail immediately without network calls
    HALF_OPEN = "HALF_OPEN" # Probing: allowing a limited trial to verify host recovery


class CircuitBreakerOpenError(HRMSConnectorError):
    """Raised when an outbound request is rejected because the circuit breaker is OPEN."""

    def __init__(self, message: str, retry_after: float = 30.0, vendor: Optional[str] = None):
        super().__init__(message=message, status_code=503, vendor=vendor)
        self.retry_after = retry_after


class TokenBucketRateLimiter:
    """Async token-bucket rate limiter to enforce strict requests-per-second ceilings."""

    def __init__(self, rate: float = 10.0, capacity: float = 20.0) -> None:
        self.rate = max(0.1, rate)
        self.capacity = max(1.0, capacity)
        self._tokens = self.capacity
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: float = 1.0) -> float:
        """Acquire execution permits, sleeping asynchronously if rate budget is exhausted.

        Returns the number of seconds waited.
        """
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_refill
            self._tokens = min(self.capacity, self._tokens + (elapsed * self.rate))
            self._last_refill = now

            waited = 0.0
            if self._tokens < tokens:
                needed = tokens - self._tokens
                waited = needed / self.rate
                await asyncio.sleep(waited)
                self._tokens = 0.0
                self._last_refill = time.monotonic()
            else:
                self._tokens -= tokens

            return waited


class CircuitBreaker:
    """Enterprise 3-state circuit breaker to protect against cascading host outages."""

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout_seconds: float = 30.0,
        half_open_successes: int = 2,
        vendor_name: str = "generic",
    ) -> None:
        self.failure_threshold = max(1, failure_threshold)
        self.recovery_timeout = max(0.01, recovery_timeout_seconds)
        self.half_open_success_threshold = max(1, half_open_successes)
        self.vendor_name = vendor_name

        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._consecutive_successes = 0
        self._opened_at: float = 0.0
        self._lock = asyncio.Lock()

    @property
    def state(self) -> CircuitState:
        now = time.monotonic()
        if self._state == CircuitState.OPEN:
            if now - self._opened_at >= self.recovery_timeout:
                return CircuitState.HALF_OPEN
        return self._state

    async def before_request(self) -> None:
        """Validate whether outbound request should be permitted or fast-failed."""
        async with self._lock:
            current_state = self.state
            if current_state == CircuitState.OPEN:
                remaining = max(0.0, self.recovery_timeout - (time.monotonic() - self._opened_at))
                raise CircuitBreakerOpenError(
                    message=f"Circuit breaker for host '{self.vendor_name}' is OPEN. Retry in {remaining:.1f}s",
                    retry_after=remaining,
                    vendor=self.vendor_name,
                )
            elif current_state == CircuitState.HALF_OPEN:
                self._state = CircuitState.HALF_OPEN

    async def record_success(self) -> None:
        """Record a successful response from the host."""
        async with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                self._consecutive_successes += 1
                if self._consecutive_successes >= self.half_open_success_threshold:
                    self._state = CircuitState.CLOSED
                    self._consecutive_failures = 0
                    self._consecutive_successes = 0
                    logger.info("[CircuitBreaker:%s] Host recovered. State transitioned to CLOSED.", self.vendor_name)
            elif self._state == CircuitState.CLOSED:
                self._consecutive_failures = 0

    async def record_failure(self, error: Exception) -> None:
        """Record a host connection error, 5xx server fault, or timeout."""
        async with self._lock:
            self._consecutive_failures += 1
            logger.warning(
                "[CircuitBreaker:%s] Failure recorded (%d/%d): %s",
                self.vendor_name,
                self._consecutive_failures,
                self.failure_threshold,
                error,
            )

            if self._state in (CircuitState.CLOSED, CircuitState.HALF_OPEN):
                if self._consecutive_failures >= self.failure_threshold or self._state == CircuitState.HALF_OPEN:
                    self._state = CircuitState.OPEN
                    self._opened_at = time.monotonic()
                    self._consecutive_successes = 0
                    logger.error(
                        "[CircuitBreaker:%s] Threshold exceeded! Circuit tripped to OPEN for %.1fs",
                        self.vendor_name,
                        self.recovery_timeout,
                    )
