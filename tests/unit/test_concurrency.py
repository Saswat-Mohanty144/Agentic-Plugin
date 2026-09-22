"""Unit tests for idempotency deduplication, optimistic concurrency, and saga rollbacks."""

import asyncio
from typing import List

import pytest

from hrms_plugin.connectors.base import HRMSConflictError
from hrms_plugin.connectors.compensation import SagaCoordinator
from hrms_plugin.connectors.concurrency import (
    IdempotencyManager,
    OptimisticConcurrencyManager,
    compute_idempotency_key,
)
from hrms_plugin.schema.canonical import EntityType


def test_idempotency_key_deterministic():
    """Verify that identical actions and payloads generate identical SHA-256 keys."""
    payload1 = {"title": "Senior AI Engineer", "department": "Engineering", "salary": 200000}
    payload2 = {"department": "Engineering", "salary": 200000, "title": "Senior AI Engineer"}  # reordered keys

    key1 = compute_idempotency_key(EntityType.REQUISITION, "create", payload1, actor_id="usr_01")
    key2 = compute_idempotency_key(EntityType.REQUISITION, "create", payload2, actor_id="usr_01")

    assert key1 == key2
    assert len(key1) == 64  # valid SHA-256 hex string


def test_idempotency_key_differs_on_actor_or_payload():
    """Verify that different actors or payloads yield different keys."""
    payload = {"title": "Recruiter"}
    key1 = compute_idempotency_key(EntityType.REQUISITION, "create", payload, actor_id="usr_01")
    key2 = compute_idempotency_key(EntityType.REQUISITION, "create", payload, actor_id="usr_02")
    key3 = compute_idempotency_key(EntityType.REQUISITION, "create", {"title": "Lead"}, actor_id="usr_01")

    assert key1 != key2
    assert key1 != key3


@pytest.mark.asyncio
async def test_idempotency_manager_acquire_and_replay():
    """Verify that duplicate requests return the cached result rather than executing twice."""
    manager = IdempotencyManager(default_ttl_seconds=10.0)
    key = "test_key_123"

    # 1. First execution acquires lock
    acquired, cached = await manager.acquire_or_get(key)
    assert acquired is True
    assert cached is None

    # 2. Complete execution and store result
    mock_entity = {"id": "req_999", "status": "CREATED"}
    manager.store_result(key, mock_entity)

    # 3. Duplicate request arrives within TTL window
    acquired_dup, cached_dup = await manager.acquire_or_get(key)
    assert acquired_dup is False
    assert cached_dup == mock_entity


@pytest.mark.asyncio
async def test_idempotency_manager_release_on_failure():
    """Verify that failed mutations release the key so subsequent calls can retry."""
    manager = IdempotencyManager(default_ttl_seconds=10.0)
    key = "failing_key_456"

    acquired, _ = await manager.acquire_or_get(key)
    assert acquired is True

    # Simulate failure
    manager.release_on_failure(key)

    # Retry should now be permitted to acquire
    reacquired, cached = await manager.acquire_or_get(key)
    assert reacquired is True
    assert cached is None
    manager.store_result(key, "success_now")


def test_optimistic_concurrency_manager():
    """Verify optimistic version checking passes on match and raises 409 on conflict."""
    # Match passes silently
    OptimisticConcurrencyManager.verify_version(
        current_version="v2",
        expected_version="v2",
        entity_id="emp_01",
        entity_type=EntityType.EMPLOYEE,
    )

    # None passes (no check requested)
    OptimisticConcurrencyManager.verify_version(
        current_version="v2",
        expected_version=None,
        entity_id="emp_01",
        entity_type=EntityType.EMPLOYEE,
    )

    # Mismatch raises HRMSConflictError with 409
    with pytest.raises(HRMSConflictError) as exc_info:
        OptimisticConcurrencyManager.verify_version(
            current_version="v3",
            expected_version="v2",
            entity_id="emp_01",
            entity_type=EntityType.EMPLOYEE,
        )
    assert exc_info.value.status_code == 409
    assert "Optimistic concurrency conflict" in str(exc_info.value)


@pytest.mark.asyncio
async def test_saga_coordinator_lifo_rollback():
    """Verify that multi-step saga rollbacks execute in reverse order (LIFO)."""
    coordinator = SagaCoordinator(saga_id="test_saga_recruitment")
    execution_order: List[str] = []

    def undo_step1(record_id: str):
        execution_order.append(f"undo_step1:{record_id}")

    async def undo_step2(requisition_id: str):
        await asyncio.sleep(0.01)
        execution_order.append(f"undo_step2:{requisition_id}")

    coordinator.register("delete_interview_slot", undo_step1, "slot_42")
    coordinator.register("cancel_requisition", undo_step2, "req_104")

    assert coordinator.pending_steps_count == 2

    # Execute rollback
    results = await coordinator.rollback()

    assert len(results) == 2
    assert all(r.success for r in results)
    # LIFO: step 2 unwound first, then step 1
    assert execution_order == ["undo_step2:req_104", "undo_step1:slot_42"]
    assert coordinator.pending_steps_count == 0


@pytest.mark.asyncio
async def test_saga_coordinator_commit():
    """Verify that commit clears the compensation stack upon successful workflow."""
    coordinator = SagaCoordinator()
    coordinator.register("step1", lambda: None)
    assert coordinator.pending_steps_count == 1

    coordinator.commit()
    assert coordinator.pending_steps_count == 0

    results = await coordinator.rollback()
    assert len(results) == 0
