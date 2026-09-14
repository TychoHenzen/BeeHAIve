from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import replace
from typing import Any

from .helpers import compact_context, normalize_problem_id, now
from .routing_error import RoutingError
from .routing_result import RoutingResult
from .routing_state import RoutingState
from .routing_status import RoutingStatus


class RouterLifecycleMixin:
    def begin(self: Any, problem_id: str) -> RoutingResult:
        normalized_id = normalize_problem_id(problem_id)
        if self.store.get_problem(normalized_id) is not None:
            raise RoutingError(f"Routing problem {normalized_id} already exists")
        timestamp = now()
        state = RoutingState(
            problem_id=normalized_id,
            status=RoutingStatus.ACTIVE,
            current_tier=self.config.writer.tier,
            triage_index=0,
            consecutive_failures=0,
            bounce_count=0,
            round=0,
            total_tokens=0,
            total_cost=0.0,
            recursive_spawn_depth=0,
            last_failure_context=None,
            required_action=None,
            next_reason="routine",
            created_at=timestamp,
            updated_at=timestamp,
        )
        self.store.create_problem(state)
        return RoutingResult(state, self._decision(state), None, ())

    def snapshot(self: Any, problem_id: str) -> RoutingResult:
        normalized_id = normalize_problem_id(problem_id)
        state = self.store.get_problem(normalized_id)
        if state is None:
            raise RoutingError(f"Unknown routing problem: {normalized_id}")
        return RoutingResult(
            state,
            self._decision(state),
            None,
            self.store.get_attempts(normalized_id),
        )

    def reopen_resolved(self: Any, problem_id: str, reason: str) -> RoutingResult:
        """Make a resolved route retryable after its run result was not durable."""

        normalized_id = normalize_problem_id(problem_id)
        context = compact_context(reason)
        if not context:
            raise RoutingError("A routing recovery reason is required")
        with self.coordinate(normalized_id):
            current = self.snapshot(normalized_id)
            if current.state.status is not RoutingStatus.RESOLVED:
                return current
            state = self.store.reopen_problem(
                normalized_id,
                current.state.round,
                replace(
                    current.state,
                    status=RoutingStatus.ACTIVE,
                    current_tier=self.config.writer.tier,
                    triage_index=0,
                    consecutive_failures=0,
                    required_action=None,
                    last_failure_context=context,
                    next_reason="retry after run persistence failure",
                    updated_at=now(),
                ),
            )
            return RoutingResult(
                state,
                self._decision(state),
                None,
                self.store.get_attempts(normalized_id),
            )

    def reopen_human_handoff(self: Any, problem_id: str, reason: str) -> RoutingResult:
        """Make a human handoff retryable after an operator answers its question."""

        normalized_id = normalize_problem_id(problem_id)
        context = compact_context(reason)
        if not context:
            raise RoutingError("A routing recovery reason is required")
        with self.coordinate(normalized_id):
            current = self.snapshot(normalized_id)
            if current.state.status is not RoutingStatus.HUMAN_HANDOFF:
                return current
            state = self.store.reopen_problem(
                normalized_id,
                current.state.round,
                replace(
                    current.state,
                    status=RoutingStatus.ACTIVE,
                    current_tier=self.config.writer.tier,
                    triage_index=0,
                    consecutive_failures=0,
                    required_action=None,
                    last_failure_context=context,
                    next_reason="retry after operator answer",
                    updated_at=now(),
                ),
            )
            return RoutingResult(
                state,
                self._decision(state),
                None,
                self.store.get_attempts(normalized_id),
            )

    @contextmanager
    def coordinate(self: Any, problem_id: str) -> Generator[None]:
        """Serialize external run transitions with model execution."""

        normalized_id = normalize_problem_id(problem_id)
        with self._execution_locks.acquire(normalized_id):
            yield
