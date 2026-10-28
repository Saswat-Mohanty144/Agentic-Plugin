"""Saga rollback coordinator for multi-step atomic agent workflows."""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class CompensatingStep:
    """Represents an undo operation registered during a distributed saga workflow."""

    name: str
    undo_fn: Callable[..., Any]
    args: tuple[Any, ...] = field(default_factory=tuple)
    kwargs: dict[str, Any] = field(default_factory=dict)
    description: Optional[str] = None


@dataclass
class RollbackResult:
    """Outcome of executing a compensating step."""

    step_name: str
    success: bool
    error: Optional[str] = None


class SagaCoordinator:
    """Tracks and executes compensating transactions in Last-In-First-Out (LIFO) order.

    If a multi-step workflow fails halfway through (e.g., job requisition created,
    but candidate sourcing or asset assignment fails), the coordinator unwinds
    all previously completed steps safely.
    """

    def __init__(self, saga_id: Optional[str] = None) -> None:
        self.saga_id = saga_id or "saga_default"
        self._steps: List[CompensatingStep] = []

    def register(
        self,
        name: str,
        undo_fn: Callable[..., Any],
        *args: Any,
        description: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        """Register a compensating step to be executed in reverse if the saga fails."""
        step = CompensatingStep(
            name=name,
            undo_fn=undo_fn,
            args=args,
            kwargs=kwargs,
            description=description,
        )
        self._steps.append(step)
        logger.debug("[saga:%s] registered compensating step '%s'", self.saga_id, name)

    async def rollback(self) -> List[RollbackResult]:
        """Execute all registered compensating steps in reverse order (LIFO)."""
        results: List[RollbackResult] = []
        logger.warning("[saga:%s] initiating rollback of %d step(s)", self.saga_id, len(self._steps))

        while self._steps:
            step = self._steps.pop()
            logger.info("[saga:%s] rolling back step '%s'...", self.saga_id, step.name)
            try:
                if inspect.iscoroutinefunction(step.undo_fn):
                    await step.undo_fn(*step.args, **step.kwargs)
                else:
                    await asyncio.to_thread(step.undo_fn, *step.args, **step.kwargs)
                results.append(RollbackResult(step_name=step.name, success=True))
                logger.info("[saga:%s] successfully rolled back step '%s'", self.saga_id, step.name)
            except Exception as e:
                err_msg = str(e)
                logger.error("[saga:%s] FAILED to roll back step '%s': %s", self.saga_id, step.name, err_msg)
                results.append(RollbackResult(step_name=step.name, success=False, error=err_msg))

        return results

    def commit(self) -> None:
        """Clear all registered compensating steps when the saga successfully finishes."""
        step_count = len(self._steps)
        self._steps.clear()
        logger.debug("[saga:%s] committed successfully; cleared %d step(s)", self.saga_id, step_count)

    @property
    def pending_steps_count(self) -> int:
        """Number of registered compensating steps waiting to be executed or committed."""
        return len(self._steps)
