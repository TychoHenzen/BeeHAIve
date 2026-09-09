"""Application service coordinating provider work and durable state."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from threading import Lock

from .models import HandoffRequest, RunState, RunStatus, Stage
from .provider import ProjectProvider
from .routing import (
    AttemptOutcome,
    ModelExecutor,
    ModelRouter,
    RoutingError,
    RoutingResult,
    RoutingStatus,
)
from .storage import OrchestratorStore, StoreError


class _KeyedLockEntry:
    def __init__(self) -> None:
        self.lock = Lock()
        self.users = 0


class _KeyedLockManager:
    def __init__(self) -> None:
        self._entries: dict[str, _KeyedLockEntry] = {}
        self._guard = Lock()

    @contextmanager
    def acquire(self, key: str) -> Generator[None]:
        with self._guard:
            entry = self._entries.get(key)
            if entry is None:
                entry = _KeyedLockEntry()
                self._entries[key] = entry
            entry.users += 1
        entry.lock.acquire()
        try:
            yield
        finally:
            entry.lock.release()
            with self._guard:
                entry.users -= 1
                if entry.users == 0 and self._entries.get(key) is entry:
                    del self._entries[key]


class Orchestrator:
    """Coordinates discovery, repository writer claims, and handoffs."""

    def __init__(
        self,
        store: OrchestratorStore,
        provider: ProjectProvider,
        model_router: ModelRouter | None = None,
        model_executor: ModelExecutor | None = None,
    ) -> None:
        self.store = store
        self.provider = provider
        self.model_router = model_router
        self.model_executor = model_executor
        self._handoff_locks = _KeyedLockManager()

    def synchronize(self, project_id: str) -> dict[str, object]:
        snapshot = self.provider.discover_project(project_id)
        self.store.sync_project(snapshot)
        return self.store.project_state(project_id)

    def claim(
        self,
        project_id: str,
        repository: str,
        owner_id: str,
        lease_token: str | None = None,
    ) -> RunState | None:
        return self.store.claim_next(project_id, repository, owner_id, lease_token)

    def advance(self, run_id: str, target: Stage, lease_token: str) -> RunState:
        run = self.store.advance(run_id, target, lease_token)
        if target is Stage.IMPLEMENT:
            self._ensure_routing_problem(run.run_id)
        return run

    def renew_lease(self, run_id: str, lease_token: str) -> RunState:
        return self.store.renew_lease(run_id, lease_token)

    def run_implementation_attempt(
        self, run_id: str, lease_token: str
    ) -> RoutingResult:
        """Execute the model selected for an active implementation run."""

        if self.model_router is None or self.model_executor is None:
            raise StoreError("A model router and executor are required")
        run = self.store.renew_lease(run_id, lease_token)
        if run.status is not RunStatus.ACTIVE or run.stage is not Stage.IMPLEMENT:
            raise StoreError("Only an active implementation run can execute a model")
        self._ensure_routing_problem(run_id)
        try:
            return self.model_router.execute(run_id, self.model_executor)
        except RoutingError as exc:
            raise StoreError(str(exc)) from exc

    def handoff(
        self,
        run_id: str,
        branch: str,
        base_branch: str | None,
        body: str,
        lease_token: str,
    ) -> RunState:
        with self._routing_coordination(run_id), self._handoff_locks.acquire(run_id):
            return self._handoff_locked(run_id, branch, base_branch, body, lease_token)

    def _handoff_locked(
        self,
        run_id: str,
        branch: str,
        base_branch: str | None,
        body: str,
        lease_token: str,
    ) -> RunState:
        run = self.store.get_run(run_id)
        if run is None:
            raise StoreError(f"Unknown run: {run_id}")
        if run.status is RunStatus.COMPLETED:
            self.store.renew_lease(run_id, lease_token)
            return run
        if run.status is not RunStatus.ACTIVE or run.stage is not Stage.IMPLEMENT:
            raise StoreError("Only an active implementation run can create a handoff")
        pending = self.store.pending_handoff(run_id, lease_token)
        if pending is not None:
            if (
                pending.branch != branch
                or pending.body != body
                or (base_branch is not None and pending.base_branch != base_branch)
            ):
                raise StoreError("Handoff request does not match the persisted intent")
            intent = self.store.prepare_handoff(
                run_id,
                pending.branch,
                pending.base_branch,
                pending.body,
                lease_token,
            )
        else:
            resolved_base_branch = self.provider.validate_handoff(
                run.repository, branch, base_branch
            )
            intent = self.store.prepare_handoff(
                run_id, branch, resolved_base_branch, body, lease_token
            )
        if intent.run.status is RunStatus.COMPLETED:
            return intent.run
        result = self.provider.create_handoff(
            HandoffRequest(
                project_id=intent.run.project_id,
                repository=intent.run.repository,
                pbi_number=intent.run.pbi_number,
                title=intent.run.title,
                branch=intent.branch,
                base_branch=intent.base_branch,
                body=intent.body,
                run_id=intent.run.run_id,
            )
        )
        self._complete_routing_problem(run_id)
        completed = self.store.record_handoff(
            run_id,
            result.branch,
            result.pull_request_url,
            result.pull_request_number,
            lease_token,
        )
        return completed

    def fail(
        self,
        run_id: str,
        error: str,
        lease_token: str,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        recursive_spawn_depth: int = 0,
    ) -> RunState:
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
    def _routing_coordination(self, run_id: str) -> Generator[None]:
        if self.model_router is None:
            yield
            return
        with self.model_router.coordinate(run_id):
            yield

    def _fail_with_routing(
        self,
        run_id: str,
        error: str,
        lease_token: str,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        recursive_spawn_depth: int = 0,
    ) -> RunState:
        failed, transitioned = self.store.fail_with_transition(
            run_id, error, lease_token
        )
        if (
            self.model_router is not None
            and transitioned
            and failed.stage is Stage.IMPLEMENT
        ):
            self._ensure_routing_problem(run_id)
            self._record_failure_routing(
                run_id,
                error,
                input_tokens,
                output_tokens,
                recursive_spawn_depth,
            )
        elif (
            self.model_router is not None
            and not transitioned
            and failed.stage is Stage.IMPLEMENT
        ):
            self._ensure_routing_problem(run_id)
            if not self.model_router.store.get_attempts(run_id):
                self._record_failure_routing(
                    run_id,
                    error,
                    input_tokens,
                    output_tokens,
                    recursive_spawn_depth,
                )
        return failed

    def stop(self, run_id: str, reason: str = "Stopped by operator") -> RunState:
        """Apply an authenticated operator stop without a worker lease."""

        return self.store.stop(run_id, reason)

    def _ensure_routing_problem(self, run_id: str) -> None:
        if self.model_router is None:
            return
        if self.model_router.store.get_problem(run_id) is not None:
            return
        try:
            self.model_router.begin(run_id)
        except RoutingError as exc:
            raise StoreError(str(exc)) from exc

    def _complete_routing_problem(self, run_id: str) -> None:
        if self.model_router is None:
            return
        self._ensure_routing_problem(run_id)
        state = self.model_router.store.get_problem(run_id)
        if state is None or state.status is RoutingStatus.RESOLVED:
            return
        try:
            self.model_router.record(run_id, AttemptOutcome.SUCCESS)
        except RoutingError as exc:
            raise StoreError(str(exc)) from exc

    def _record_failure_routing(
        self,
        run_id: str,
        error: str,
        input_tokens: int,
        output_tokens: int,
        recursive_spawn_depth: int,
    ) -> None:
        if self.model_router is None:  # pragma: no cover - guarded by callers
            return
        try:
            self.model_router.record(
                run_id,
                AttemptOutcome.FAILURE,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                failure_context=error,
                recursive_spawn_depth=recursive_spawn_depth,
            )
        except RoutingError as exc:
            state = self.model_router.store.get_problem(run_id)
            if (
                state is not None
                and state.status is not RoutingStatus.RESOLVED
                and self.model_router.store.get_attempts(run_id)
            ):
                return
            raise StoreError(str(exc)) from exc
