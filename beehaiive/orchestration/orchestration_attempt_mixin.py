from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime
from threading import Event, Thread
from typing import Any, cast

from beehaiive.operator_notifications import (
    dispatch_pending_operator_notifications,
)

from ..contracts import TaskContract, TaskResult
from ..graph import GraphDefinition
from ..graph_execution import (
    GraphExecutionPolicy,
    GraphExecutionService,
    GraphTransition,
)
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
    def run_graph_node(
        self: Any,
        definition: GraphDefinition,
        *,
        run_id: str,
        lease_token: str,
        task_id: str,
        node_id: str,
        contract: TaskContract,
        step: int,
        attempt: int,
        started_at: datetime | None = None,
        policy: GraphExecutionPolicy | None = None,
        guard: Callable[[], None] | None = None,
    ) -> GraphTransition:
        return GraphExecutionService(self.store).execute_orchestrated_node(
            self,
            definition,
            run_id=run_id,
            lease_token=lease_token,
            task_id=task_id,
            node_id=node_id,
            contract=contract,
            step=step,
            attempt=attempt,
            started_at=started_at,
            policy=policy,
            guard=guard,
        )

    def run_implementation_attempt(
        self: Any,
        run_id: str,
        lease_token: str,
        model_override: str | None = None,
        *,
        routing_problem_id: str | None = None,
        task_contract: TaskContract | None = None,
    ) -> RoutingResult:
        """Execute the model selected for an active implementation run."""

        if self.model_router is None or self.model_executor is None:
            raise StoreError("A model router and executor are required")
        run = self.store.renew_lease(run_id, lease_token)
        if run.status is not RunStatus.ACTIVE or run.stage is not Stage.IMPLEMENT:
            raise StoreError("Only an active implementation run can execute a model")
        problem_id = routing_problem_id or run_id
        self._ensure_routing_problem(problem_id)
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
            contract = task_contract or self._task_contract_for_run(run)
            self.store.ensure_task_contract(run_id, contract, lease_token)
            configure_contract = getattr(self.model_executor, "set_task_contract", None)
            if callable(configure_contract):
                configure_contract(run_id, contract)

            missing_task_result = False

            def persist_task_result(execution: ModelExecution) -> TaskResult:
                nonlocal missing_task_result
                task_result = execution.task_result
                if not isinstance(task_result, TaskResult):
                    missing_task_result = True
                    task_result = TaskResult.invalid(
                        execution.failure_context
                        or "Executor did not return a structured task result"
                    )
                self.store.record_task_result(run_id, task_result, lease_token)
                return task_result

            routing = self.model_router.execute(
                problem_id,
                self.model_executor,
                before_record=validate_execution,
                persist_task_result=persist_task_result,
                model_override=model_override,
            )
            if routing_problem_id is not None and missing_task_result:
                routing = replace(routing, task_result=None)
            if routing.state.status is RoutingStatus.HUMAN_HANDOFF:
                task_result = routing.task_result
                kind = (
                    "question"
                    if task_result is not None
                    and task_result.outcome.value == "question"
                    else "routing_exhausted"
                )
                question = (
                    task_result.question
                    if task_result is not None and task_result.question
                    else task_result.required_action
                    if task_result is not None and task_result.required_action
                    else routing.state.required_action or "Human action is required"
                )
                self.store.await_operator(
                    run_id,
                    lease_token,
                    kind=kind,
                    question=question,
                    evidence=task_result.evidence if task_result is not None else {},
                )
                dispatch_pending_operator_notifications(self.store, run_id=run_id)
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
                contract = TaskContract.from_dict(run.task_contract)
                return self._task_contract_with_operator_answer(
                    contract, run.task_answer
                )
        if self.model_executor is not None:
            builder = getattr(self.model_executor, "build_task_contract", None)
            if callable(builder):
                contract = builder(run)
                if isinstance(contract, TaskContract):
                    return self._task_contract_with_operator_answer(
                        contract, run.task_answer
                    )
                raise StoreError("Model executor returned an invalid task contract")
        contract = TaskContract.inventory(
            run.repository,
            run.pbi_number,
            run.title,
            answer=run.task_answer,
        )
        return self._task_contract_with_operator_answer(contract, run.task_answer)

    @staticmethod
    def _task_contract_with_operator_answer(
        contract: TaskContract, answer: str | None
    ) -> TaskContract:
        if answer is None:
            return contract
        contract_data = contract.as_dict()
        inputs = dict(cast(Mapping[str, object], contract_data["inputs"]))
        inputs["answer"] = answer
        contract_data["inputs"] = inputs
        return TaskContract.from_dict(contract_data)

    def answer_task_question(self: Any, run_id: str, answer: str) -> RunState:
        run = self.store.answer_task_question(run_id, answer)
        return self._resume_answered_question(run_id, run)

    def answer_operator_question(
        self: Any,
        run_id: str,
        *,
        question_id: str,
        revision: int,
        answer: str,
        authorization_method: str,
        operator_role: str,
    ) -> RunState:
        question = self.store.answer_operator_question(
            run_id,
            question_id=question_id,
            revision=revision,
            answer=answer,
            authorization_method=authorization_method,
            operator_role=operator_role,
        )
        run = self.store.get_run(run_id)
        if run is None:
            raise StoreError(f"Unknown run: {run_id}")
        if (
            question["status"] == "answered"
            and run.status is not RunStatus.AWAITING_OPERATOR
        ):
            return run
        return self._resume_answered_question(run_id, run)

    def _resume_answered_question(self: Any, run_id: str, run: RunState) -> RunState:
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
