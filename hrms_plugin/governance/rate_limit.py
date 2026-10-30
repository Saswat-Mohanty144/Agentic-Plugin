"""Per-tenant request rate limit for LLM- and voice-backed routes (Q4, batch QE-3).

Token bucket per tenant: capacity = requests_per_minute, refilled continuously. The limit comes from the tenant's
policy (owner input); no limit configured means unlimited, the same rule as the daily token budget (piece H4).
In-process: with several replicas each enforces its own bucket.
"""

from __future__ import annotations

import math
import threading
import time
from typing import Callable, Dict, Optional, Tuple

from fastapi import Request

from hrms_plugin.security.http_auth import ApiError
from hrms_plugin.security.rbac import AuthContext

LimitResolver = Callable[[str], Optional[int]]


class TenantRateLimiter:
    def __init__(self, resolve: LimitResolver, clock: Callable[[], float] = time.monotonic):
        self._resolve = resolve
        self._clock = clock
        self._buckets: Dict[str, Tuple[float, float]] = {}  # tenant -> (tokens, last refill)
        self._lock = threading.Lock()

    def limit_for(self, tenant_id: str) -> Optional[int]:
        return self._resolve(tenant_id)

    def check(self, tenant_id: str) -> Optional[float]:
        """None when the request may proceed; otherwise the seconds until one more request is allowed."""
        limit = self._resolve(tenant_id)
        if limit is None:
            return None
        rate = limit / 60.0
        with self._lock:
            now = self._clock()
            tokens, last = self._buckets.get(tenant_id, (float(limit), now))
            tokens = min(float(limit), tokens + (now - last) * rate)
            if tokens >= 1.0:
                self._buckets[tenant_id] = (tokens - 1.0, now)
                return None
            self._buckets[tenant_id] = (tokens, now)
            return (1.0 - tokens) / rate


def enforce_rate_limit(request: Request, auth: AuthContext) -> None:
    """QE-3: per-tenant requests per minute on LLM- and voice-backed routes; checked after authentication."""
    retry = request.app.state.rate_limiter.check(auth.tenant_id)
    if retry is not None:
        raise ApiError(429, {"error": "RATE_LIMITED"}, headers={"Retry-After": str(max(1, math.ceil(retry)))})


