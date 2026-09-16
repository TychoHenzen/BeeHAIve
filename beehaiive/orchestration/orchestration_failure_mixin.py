from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

from beehaiive.operator_notifications import (
    dispatch_pending_operator_notifications,
)

from ..models import (
    ProjectSnapshot,
    RoutingFailure,
    RunState,
    Stage,
)
from ..routing import (
    AttemptOutcome,
    RoutingError,
    RoutingStatus,
)
from ..storage import StoreError


class OrchestrationFailureMixin:
    def fail(
        self: Any,
        run_id: str,
        error: str,
        lease_token: str,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        recursive_spawn_depth: int = 0,
    ) -> RunState:
        self._cancel_worker(run_id)
        with self._routing_coordination(run_id), self._handoff_locks.acquire(run_id):
            return self._fail_with_routing(
                run_id,
                error,
                lease_token,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                recursive_spawn_depth=recursive_spawn_depth,
            )

    @contextmanager
    def _routing_coordination(self: Any, run_id: str) -> Generator[None]:
        if self.model_router is None:
            yield
            return
        with self.model_router.coordinate(run_id):
            yield

    def _fail_with_routing(
        self: Any,
        run_id: str,
        error: str,
        lease_token: str,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        recursive_spawn_depth: int = 0,
    ) -> RunState:
        run_before = self.store.get_run(run_id)
        route_failure = (
            self.model_router is not None
            and run_before is not None
            and run_before.stage is Stage.IMPLEMENT
        )
        failed, _ = self.store.fail_with_transition(
            run_id,
            error,
            lease_token,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            recursive_spawn_depth=recursive_spawn_depth,
            route_failure=route_failure,
        )
        if self.model_router is not None and failed.stage is Stage.IMPLEMENT:
            self._ensure_routing_problem(run_id)
            for failure in self.store.pending_routing_failures(run_id):
                self._record_failure_routing(failure)
        return self.store.get_run(run_id) or failed

    def stop(self: Any, run_id: str, reason: str = "Stopped by operator") -> RunState:
        """Apply an authenticated operator stop without a worker lease."""

        stopped = self.store.stop(run_id, reason)
        self._cancel_worker(run_id)
        return stopped

    def recover_routing_problem(self: Any, run_id: str, reason: str) -> None:
        if self.model_router is None:
            return
        try:
            self.model_router.reopen_resolved(run_id, reason)
        except RoutingError as exc:
            raise StoreError(str(exc)) from exc

    def _cancel_worker(self: Any, run_id: str) -> None:
        if self._worker_canceller is not None:
            self._worker_canceller(run_id)

    def _cancel_removed_workers(self: Any, snapshot: ProjectSnapshot) -> None:
        active_runs = self.store.active_runs_for_project(snapshot.project_id)
        visible_pbis = {
            (repository.name, pbi.number)
            for repository in snapshot.repositories
            for pbi in repository.pbis
        }
        for run in active_runs:
            if (run.repository, run.pbi_number) not in visible_pbis:
                self._cancel_worker(run.run_id)

    def _ensure_routing_problem(self: Any, run_id: str) -> None:
        if self.model_router is None:
            return
        if self.model_router.store.get_problem(run_id) is not None:
            return
        try:
            self.model_router.begin(run_id)
        except RoutingError as exc:
            raise StoreError(str(exc)) from exc

    def _complete_routing_problem(self: Any, run_id: str) -> None:
        if self.model_router is None:
            return
        self._ensure_routing_problem(run_id)
        state = self.model_router.store.get_problem(run_id)
        if state is None or state.status is RoutingStatus.RESOLVED:
            return
        try:
            result = self.model_router.record(run_id, AttemptOutcome.SUCCESS)
        except RoutingError as exc:
            raise StoreError(str(exc)) from exc
        if result.state.status is not RoutingStatus.RESOLVED:
            raise StoreError("Routing handoff requires human action")

    def _record_failure_routing(self: Any, failure: RoutingFailure) -> None:
        if self.model_router is None:  # pragma: no cover - guarded by callers
            return
        try:
            result = self.model_router.record(
                failure.run_id,
                AttemptOutcome.FAILURE,
                input_tokens=failure.input_tokens,
                output_tokens=failure.output_tokens,
                failure_context=failure.error,
                recursive_spawn_depth=failure.recursive_spawn_depth,
                transition_id=failure.transition_id,
            )
            if result.state.status is RoutingStatus.HUMAN_HANDOFF:
                question = (
                    result.state.required_action
                    or failure.error
                    or "Routing is exhausted and requires operator action"
                )
                self.store.await_operator_after_failure(
                    failure.run_id,
                    kind="routing_exhausted",
                    question=question,
                    evidence={},
                )
                dispatch_pending_operator_notifications(
                    self.store, run_id=failure.run_id
                )
        except RoutingError as exc:
            if self.model_router.store.has_transition(failure.transition_id):
                self.store.mark_routing_failure_processed(failure.transition_id)
                return
            raise StoreError(str(exc)) from exc
        self.store.mark_routing_failure_processed(failure.transition_id)
