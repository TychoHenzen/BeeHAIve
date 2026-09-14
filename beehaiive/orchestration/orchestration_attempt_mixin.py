from __future__ import annotations

from collections.abc import Mapping
from threading import Event, Thread
from typing import Any, cast

from ..contracts import TaskContract, TaskResult
from ..models import (
    RunState,
    RunStatus,
    Stage,
)
from ..routing import (
    ModelExecution,
    RoutingError,
    RoutingResult,
    RoutingStatus,
)
from ..storage import StoreError


class OrchestrationAttemptMixin:
    def run_implementation_attempt(
        self: Any, run_id: str, lease_token: str
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

    def _task_contract_for_run(self: Any, run: RunState) -> TaskContract:
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

    def answer_task_question(self: Any, run_id: str, answer: str) -> RunState:
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
