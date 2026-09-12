"""Application service coordinating provider work and durable state."""

from __future__ import annotations

from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from threading import Event, Lock, Thread
from typing import cast

from .checks import blocking_check_failure
from .contracts import TaskContract, TaskResult
from .models import (
    HandoffRequest,
    ProjectSnapshot,
    RoutingFailure,
    RunState,
    RunStatus,
    Stage,
)
from .provider import ProjectProvider
from .routing import (
    AttemptOutcome,
    ModelExecution,
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
        self._worker_canceller: Callable[[str], None] | None = None

    def register_worker_canceller(self, canceller: Callable[[str], None]) -> None:
        self._worker_canceller = canceller

    def synchronize(
        self, project_id: str, *, force_refresh: bool = False
    ) -> dict[str, object]:
        if force_refresh:
            invalidate = getattr(self.provider, "invalidate_discovery_cache", None)
            if callable(invalidate):
                invalidate()
        snapshot = self.provider.discover_project(project_id)
        self._cancel_removed_workers(snapshot)
        self.store.sync_project(snapshot)
        self._route_blocking_check_failures(snapshot)
        return self.store.project_state(project_id)

    def claim(
        self,
        project_id: str,
        repository: str,
        owner_id: str,
        lease_token: str | None = None,
        *,
        expected_run_id: str | None = None,
        agent_session: tuple[str, str] | None = None,
    ) -> RunState | None:
        return self.store.claim_next(
            project_id,
            repository,
            owner_id,
            lease_token,
            expected_run_id=expected_run_id,
            agent_session=agent_session,
        )

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
        execution_token = self.store.claim_execution(run_id, lease_token)
        stop_heartbeat = Event()
        heartbeat_errors: list[StoreError] = []

        def heartbeat() -> None:
            while not stop_heartbeat.wait(self.store.lease_heartbeat_seconds):
                try:
                    self.store.heartbeat_execution(run_id, lease_token, execution_token)
                except StoreError as exc:
                    heartbeat_errors.append(exc)
                    return

        heartbeat_thread = Thread(target=heartbeat, daemon=True)
        heartbeat_thread.start()

        def validate_execution() -> None:
            if heartbeat_errors:
                raise RoutingError(str(heartbeat_errors[0]))
            try:
                self.store.validate_execution(run_id, lease_token, execution_token)
            except StoreError as exc:
                raise RoutingError(str(exc)) from exc

        try:
            contract = self._task_contract_for_run(run)
            self.store.ensure_task_contract(run_id, contract, lease_token)
            configure_contract = getattr(self.model_executor, "set_task_contract", None)
            if callable(configure_contract):
                configure_contract(run_id, contract)

            def persist_task_result(execution: ModelExecution) -> TaskResult:
                task_result = execution.task_result
                if not isinstance(task_result, TaskResult):
                    task_result = TaskResult.invalid(
                        execution.failure_context
                        or "Executor did not return a structured task result"
                    )
                self.store.record_task_result(run_id, task_result, lease_token)
                return task_result

            routing = self.model_router.execute(
                run_id,
                self.model_executor,
                before_record=validate_execution,
                persist_task_result=persist_task_result,
            )
            return routing
        except RoutingError as exc:
            raise StoreError(str(exc)) from exc
        finally:
            stop_heartbeat.set()
            heartbeat_thread.join(timeout=max(self.store.lease_heartbeat_seconds, 1.0))
            self.store.release_execution(run_id, execution_token)

    def _task_contract_for_run(self, run: RunState) -> TaskContract:
        if run.task_contract is not None:
            inputs = run.task_contract.get("inputs")
            persisted_answer: object = None
            if isinstance(inputs, Mapping):
                persisted_answer = cast(Mapping[str, object], inputs).get("answer")
            if persisted_answer == run.task_answer:
                return TaskContract.from_dict(run.task_contract)
        if self.model_executor is not None:
            builder = getattr(self.model_executor, "build_task_contract", None)
            if callable(builder):
                contract = builder(run)
                if isinstance(contract, TaskContract):
                    return contract
                raise StoreError("Model executor returned an invalid task contract")
        return TaskContract.inventory(
            run.repository,
            run.pbi_number,
            run.title,
            answer=run.task_answer,
        )

    def answer_task_question(self, run_id: str, answer: str) -> RunState:
        run = self.store.answer_task_question(run_id, answer)
        if self.model_router is not None:
            self._ensure_routing_problem(run_id)
            try:
                routing = self.model_router.reopen_human_handoff(
                    run_id, "Operator answered task question"
                )
            except RoutingError as exc:
                raise StoreError(str(exc)) from exc
            if routing.state.status is not RoutingStatus.ACTIVE:
                raise StoreError("Task question routing is not resumable")
        self.store.mark_task_question_resumed(run_id)
        return self.store.get_run(run_id) or run

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
        self._ensure_handoff_routing_allowed(run_id)
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
        return failed

    def stop(self, run_id: str, reason: str = "Stopped by operator") -> RunState:
        """Apply an authenticated operator stop without a worker lease."""

        self._cancel_worker(run_id)
        return self.store.stop(run_id, reason)

    def recover_routing_problem(self, run_id: str, reason: str) -> None:
        if self.model_router is None:
            return
        try:
            self.model_router.reopen_resolved(run_id, reason)
        except RoutingError as exc:
            raise StoreError(str(exc)) from exc

    def _cancel_worker(self, run_id: str) -> None:
        if self._worker_canceller is not None:
            self._worker_canceller(run_id)

    def _cancel_removed_workers(self, snapshot: ProjectSnapshot) -> None:
        active_runs = self.store.active_runs_for_project(snapshot.project_id)
        visible_pbis = {
            (repository.name, pbi.number)
            for repository in snapshot.repositories
            for pbi in repository.pbis
        }
        for run in active_runs:
            if (run.repository, run.pbi_number) not in visible_pbis:
                self._cancel_worker(run.run_id)

    def _route_blocking_check_failures(self, snapshot: ProjectSnapshot) -> None:
        active_runs = {
            (run.repository, run.pbi_number): run
            for run in self.store.active_runs_for_project(snapshot.project_id)
            if run.stage is Stage.IMPLEMENT and run.lease_token is not None
        }
        for repository in snapshot.repositories:
            for pbi in repository.pbis:
                failure = blocking_check_failure(pbi.metadata)
                run = active_runs.get((repository.name, pbi.number))
                if failure is None or run is None or run.lease_token is None:
                    continue
                self.fail(run.run_id, failure, run.lease_token)

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
            result = self.model_router.record(run_id, AttemptOutcome.SUCCESS)
        except RoutingError as exc:
            raise StoreError(str(exc)) from exc
        if result.state.status is not RoutingStatus.RESOLVED:
            raise StoreError("Routing handoff requires human action")

    def _ensure_handoff_routing_allowed(self, run_id: str) -> None:
        if self.model_router is None:
            return
        self._ensure_routing_problem(run_id)
        reason = self.model_router.handoff_limit_reason(run_id)
        if reason is not None:
            raise StoreError(f"Routing handoff requires human action: {reason}")

    def _record_failure_routing(self, failure: RoutingFailure) -> None:
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
                self.store.set_run_claimable(failure.run_id, False)
        except RoutingError as exc:
            if self.model_router.store.has_transition(failure.transition_id):
                self.store.mark_routing_failure_processed(failure.transition_id)
                return
            raise StoreError(str(exc)) from exc
        self.store.mark_routing_failure_processed(failure.transition_id)
