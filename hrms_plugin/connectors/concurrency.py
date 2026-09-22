"""Concurrency control and idempotency managers for HRMS mutations."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from hrms_plugin.connectors.base import HRMSConflictError
from hrms_plugin.schema.canonical import EntityType


def compute_idempotency_key(
    entity_type: EntityType,
    action: str,
    payload: Dict[str, Any],
    actor_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
) -> str:
    """Compute a deterministic SHA-256 idempotency key for an action and payload."""
    actor = actor_id or "anonymous"
    tenant = tenant_id or "default"
    serialized_payload = json.dumps(payload, sort_keys=True, default=str)
    raw = f"{tenant}:{entity_type.value}:{action.lower()}:{actor}:{serialized_payload}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass
class CachedMutationResult:
    """Stores the result of an executed mutation for idempotent replay."""

    result: Any
    created_at: float
    expires_at: float


class IdempotencyManager:
    """Thread-safe in-memory idempotency deduplicator with TTL expiration.

    Prevents double-booking, duplicate requisitions, or duplicate payroll entries
    when client retries or network blips occur.
    """

    def __init__(self, default_ttl_seconds: float = 120.0) -> None:
        self.default_ttl = default_ttl_seconds
        self._cache: Dict[str, CachedMutationResult] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()

    async def _get_key_lock(self, key: str) -> asyncio.Lock:
        async with self._global_lock:
            if key not in self._locks:
                self._locks[key] = asyncio.Lock()
            return self._locks[key]

    async def acquire_or_get(
        self,
        key: str,
    ) -> Tuple[bool, Optional[Any]]:
        """Attempt to acquire execution rights for a given idempotency key.

        Returns:
            (acquired, cached_result):
            - If (False, cached_result): Mutation was already executed; return cached result.
            - If (True, None): Caller has acquired rights to execute the mutation.
        """
        now = time.time()
        self._purge_expired(now)

        lock = await self._get_key_lock(key)
        await lock.acquire()

        if key in self._cache:
            entry = self._cache[key]
            if entry.expires_at > now:
                lock.release()
                return False, entry.result

        # Caller acquired the lock to execute
        return True, None

    def store_result(self, key: str, result: Any, ttl_seconds: Optional[float] = None) -> None:
        """Store the successful result of a mutation and release the execution lock."""
        now = time.time()
        ttl = ttl_seconds if ttl_seconds is not None else self.default_ttl
        self._cache[key] = CachedMutationResult(
            result=result,
            created_at=now,
            expires_at=now + ttl,
        )
        if key in self._locks and self._locks[key].locked():
            self._locks[key].release()

    def release_on_failure(self, key: str) -> None:
        """Release the lock without storing a result if the mutation failed."""
        if key in self._locks and self._locks[key].locked():
            self._locks[key].release()
        self._cache.pop(key, None)

    def _purge_expired(self, now: float) -> None:
        expired_keys = [k for k, v in self._cache.items() if v.expires_at <= now]
        for k in expired_keys:
            self._cache.pop(k, None)
            self._locks.pop(k, None)


class OptimisticConcurrencyManager:
    """Validates entity versions prior to executing updates to prevent lost updates."""

    @staticmethod
    def verify_version(
        current_version: Optional[str],
        expected_version: Optional[str],
        entity_id: str,
        entity_type: EntityType,
    ) -> None:
        """Verify that current version matches expected version.

        Raises:
            HRMSConflictError: If version mismatch is detected.
        """
        if expected_version is None:
            # Caller did not request optimistic locking
            return

        if current_version != expected_version:
            raise HRMSConflictError(
                message=(
                    f"Optimistic concurrency conflict on {entity_type.value} '{entity_id}': "
                    f"expected version '{expected_version}' but remote is '{current_version}'."
                ),
                status_code=409,
            )
