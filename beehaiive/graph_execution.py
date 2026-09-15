from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from math import isfinite
from types import MappingProxyType
from typing import Protocol, cast
from uuid import uuid4

from .contract_types import (
    ContractError,
    TaskContract,
    TaskOutcome,
    TaskResult,
    _bounded_value,
)
from .graph import GraphDefinition, GraphEdge, GraphNode
from .operator_notifications import dispatch_pending_operator_notifications
from .routing import RoutingStatus

MAX_GRAPH_EXECUTION_TEXT = 512
DEFAULT_GRAPH_MAX_STEPS = 128
MAX_GRAPH_MAX_STEPS = 256
GRAPH_TRANSITION_CLAIM_SECONDS = 900.0


class GraphExecutionError(ContractError):
    """Raised when a graph transition cannot be executed safely."""


class GraphTransitionStatus(StrEnum):
    ADVANCED = "advanced"
    TERMINAL = "terminal"
    PAUSED = "paused"


@dataclass(frozen=True, slots=True)
class GraphExecutionPolicy:
    max_steps: int = DEFAULT_GRAPH_MAX_STEPS
    node_timeout_seconds: float | None = None

    def __post_init__(self) -> None:
        if (
            type(self.max_steps) is not int
            or not 1 <= self.max_steps <= MAX_GRAPH_MAX_STEPS
        ):
            raise GraphExecutionError(
                f"max_steps must be between 1 and {MAX_GRAPH_MAX_STEPS}"
            )
        if self.node_timeout_seconds is not None and (
            type(self.node_timeout_seconds) not in {int, float}
            or not isfinite(self.node_timeout_seconds)
            or self.node_timeout_seconds <= 0
        ):
            raise GraphExecutionError(
                "node_timeout_seconds must be finite and positive"
            )


@dataclass(frozen=True, slots=True)
class GraphTransition:
    execution_id: str
    task_id: str
    workflow_id: str
    revision: int
    node_id: str
    step: int
    attempt: int
    outcome: TaskOutcome
    status: GraphTransitionStatus
    selected_edge: GraphEdge | None
    reason: str
    evidence: Mapping[str, object]
    created_at: str
    question: str | None = None
    required_action: str | None = None
    replay_id: str = ""

    def __post_init__(self) -> None:
        for value, label in (
            (self.execution_id, "execution_id"),
            (self.task_id, "task_id"),
            (self.workflow_id, "workflow_id"),
            (self.node_id, "node_id"),
            (self.reason, "reason"),
            (self.created_at, "created_at"),
        ):
            _validate_text(value, label)
        for value, label in (
            (self.question, "question"),
            (self.required_action, "required_action"),
        ):
            if value is not None:
                _validate_text(value, label)
        if type(self.revision) is not int or self.revision <= 0:
            raise GraphExecutionError("revision must be positive")
        if type(self.step) is not int or self.step <= 0:
            raise GraphExecutionError("step must be positive")
        if type(self.attempt) is not int or self.attempt <= 0:
            raise GraphExecutionError("attempt must be positive")
        if not isinstance(cast(object, self.outcome), TaskOutcome):
            raise GraphExecutionError("outcome must be a TaskOutcome value")
        if not isinstance(cast(object, self.status), GraphTransitionStatus):
            raise GraphExecutionError("status must be a GraphTransitionStatus value")
        if self.status is GraphTransitionStatus.ADVANCED and self.selected_edge is None:
            raise GraphExecutionError("advanced transitions require a selected edge")
        if self.selected_edge is not None and not isinstance(
            cast(object, self.selected_edge), GraphEdge
        ):
            raise GraphExecutionError("selected_edge must be a GraphEdge value")
        object.__setattr__(self, "evidence", _freeze_evidence(self.evidence))
        expected = graph_replay_id(
            self.execution_id,
            self.task_id,
            self.workflow_id,
            self.revision,
            self.node_id,
            self.step,
            self.attempt,
        )
        if self.replay_id and self.replay_id != expected:
            raise GraphExecutionError("replay_id does not match transition identity")
        object.__setattr__(self, "replay_id", expected)

    def as_dict(self) -> dict[str, object]:
        return {
            "execution_id": self.execution_id,
            "task_id": self.task_id,
            "workflow_id": self.workflow_id,
            "revision": self.revision,
            "node_id": self.node_id,
            "step": self.step,
            "attempt": self.attempt,
            "outcome": self.outcome.value,
            "status": self.status.value,
            "selected_edge": (
                self.selected_edge.as_dict() if self.selected_edge is not None else None
            ),
            "reason": self.reason,
            "evidence": _thaw_evidence(self.evidence),
            "created_at": self.created_at,
            "question": self.question,
            "required_action": self.required_action,
            "replay_id": self.replay_id,
        }


class GraphNodeExecutor(Protocol):
    def __call__(self, _node: GraphNode, _contract: TaskContract) -> TaskResult: ...


class GraphExecutionService:
    """Validate one node result, choose one declared edge, and persist it."""

    def __init__(self, store: GraphTransitionStore) -> None:
        self.store = store

    def execute_node(
        self,
        definition: GraphDefinition,
        *,
        execution_id: str,
        task_id: str,
        node_id: str,
        contract: TaskContract,
        executor: GraphNodeExecutor,
        step: int,
        attempt: int,
        started_at: datetime | None = None,
        policy: GraphExecutionPolicy | None = None,
        now: datetime | None = None,
        guard: Callable[[], None] | None = None,
        run_id: str | None = None,
        lease_token: str | None = None,
    ) -> GraphTransition:
        active_policy = policy or GraphExecutionPolicy()
        current_time = now or datetime.now(UTC)
        node = _node_for(definition, node_id)
        effective_started_at = started_at or current_time
        replay_id = graph_replay_id(
            execution_id,
            task_id,
            definition.workflow_id,
            definition.revision,
            node_id,
            step,
            attempt,
        )
        owner_id = uuid4().hex
        existing = self.store.claim_graph_transition(replay_id, owner_id)
        if existing is not None:
            self._await_operator(existing, run_id, lease_token)
            return existing
        try:
            _validate_execution_bounds(
                definition,
                step=step,
                attempt=attempt,
                started_at=effective_started_at,
                now=current_time,
                policy=active_policy,
            )
            if guard is not None:
                guard()
            try:
                raw_result = executor(node, contract)
                result = _validate_result(raw_result, contract)
            except (ContractError, TypeError, ValueError) as error:
                raise GraphExecutionError(f"Node result is invalid: {error}") from error
            finished_at = now or datetime.now(UTC)
            _validate_execution_bounds(
                definition,
                step=step,
                attempt=attempt,
                started_at=effective_started_at,
                now=finished_at,
                policy=active_policy,
            )
            if guard is not None:
                guard()
            transition = evaluate_graph_transition(
                definition,
                execution_id=execution_id,
                task_id=task_id,
                node_id=node_id,
                result=result,
                step=step,
                attempt=attempt,
                created_at=finished_at.isoformat(),
            )
            saved = self.store.record_graph_transition(transition, owner_id=owner_id)
            self._await_operator(saved, run_id, lease_token)
            return saved
        except Exception:
            self.store.release_graph_transition(replay_id, owner_id)
            raise

    def _await_operator(
        self,
        transition: GraphTransition,
        run_id: str | None,
        lease_token: str | None,
    ) -> None:
        if transition.status is not GraphTransitionStatus.PAUSED:
            return
        await_operator = getattr(self.store, "await_operator", None)
        if not callable(await_operator) or run_id is None or lease_token is None:
            return
        kind = (
            "question"
            if transition.outcome is TaskOutcome.QUESTION
            else "routing_exhausted"
        )
        await_operator(
            run_id,
            lease_token,
            kind=kind,
            question=(
                transition.question
                or transition.required_action
                or "Graph execution requires operator action"
            ),
            evidence=transition.evidence,
        )
        if hasattr(self.store, "pending_operator_notification_ids"):
            dispatch_pending_operator_notifications(self.store, run_id=run_id)

    def resume_node(
        self,
        transition: GraphTransition,
        definition: GraphDefinition,
        *,
        contract: TaskContract,
        executor: GraphNodeExecutor,
        policy: GraphExecutionPolicy | None = None,
        guard: Callable[[], None] | None = None,
        run_id: str | None = None,
        lease_token: str | None = None,
    ) -> GraphTransition:
        if transition.status is not GraphTransitionStatus.PAUSED:
            raise GraphExecutionError("only a paused graph transition can resume")
        edge = transition.selected_edge
        if edge is None:
            raise GraphExecutionError("paused transition has no declared next node")
        if (
            transition.workflow_id != definition.workflow_id
            or transition.revision != definition.revision
        ):
            raise GraphExecutionError("transition does not match graph definition")
        if (
            edge not in definition.edges
            or edge.source != transition.node_id
            or not _edge_matches(edge, transition.outcome, transition.evidence)
        ):
            raise GraphExecutionError("paused transition edge is not declared")
        return self.execute_node(
            definition,
            execution_id=transition.execution_id,
            task_id=transition.task_id,
            node_id=edge.target,
            contract=contract,
            executor=executor,
            step=transition.step + 1,
            attempt=1,
            policy=policy,
            guard=guard,
            run_id=run_id,
            lease_token=lease_token,
        )

    def execute_orchestrated_node(
        self,
        orchestrator: object,
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
        run_attempt = getattr(orchestrator, "run_implementation_attempt", None)
        if not callable(run_attempt):
            raise GraphExecutionError("orchestrator lacks a model-attempt boundary")
        routing_problem_id = "graph:" + graph_replay_id(
            run_id,
            task_id,
            definition.workflow_id,
            definition.revision,
            node_id,
            step,
            attempt,
        )

        def execute(_node: GraphNode, _contract: TaskContract) -> TaskResult:
            node_contract = _contract_for_node(_contract, _node)
            routing = run_attempt(
                run_id,
                lease_token,
                routing_problem_id=routing_problem_id,
                task_contract=node_contract,
            )
            result = getattr(routing, "task_result", None)
            if not isinstance(result, TaskResult):
                raise GraphExecutionError(
                    "Orchestrator did not return a validated task result"
                )
            state = getattr(routing, "state", None)
            status = getattr(state, "status", None)
            if status is RoutingStatus.HUMAN_HANDOFF and result.outcome not in {
                TaskOutcome.BLOCKED,
                TaskOutcome.QUESTION,
            }:
                raise GraphExecutionError(
                    "Model attempt requires human action before graph advancement"
                )
            return result

        return self.execute_node(
            definition,
            execution_id=run_id,
            task_id=task_id,
            node_id=node_id,
            contract=contract,
            executor=execute,
            step=step,
            attempt=attempt,
            started_at=started_at,
            policy=policy,
            guard=guard,
            run_id=run_id,
            lease_token=lease_token,
        )


class GraphTransitionStore(Protocol):
    def claim_graph_transition(
        self, replay_id: str, owner_id: str
    ) -> GraphTransition | None: ...

    def release_graph_transition(self, replay_id: str, owner_id: str) -> None: ...

    def graph_transition_for_replay(self, replay_id: str) -> GraphTransition | None: ...

    def record_graph_transition(
        self, transition: GraphTransition, *, owner_id: str | None = None
    ) -> GraphTransition: ...


def evaluate_graph_transition(
    definition: GraphDefinition,
    *,
    execution_id: str,
    task_id: str,
    node_id: str,
    result: TaskResult,
    step: int,
    attempt: int,
    created_at: str,
) -> GraphTransition:
    node = _node_for(definition, node_id)
    del node
    if not isinstance(cast(object, result), TaskResult):
        raise GraphExecutionError("result must be a TaskResult value")
    outcome = result.outcome
    if not isinstance(cast(object, outcome), TaskOutcome):
        raise GraphExecutionError("result outcome must be a TaskOutcome value")
    matches = tuple(
        edge
        for edge in definition.edges
        if edge.source == node_id and _edge_matches(edge, outcome, result.evidence)
    )
    if len(matches) > 1:
        raise GraphExecutionError("multiple graph edges match the task outcome")
    selected_edge = matches[0] if matches else None
    if selected_edge is None:
        status = GraphTransitionStatus.TERMINAL
        reason = "no declared edge matches the task outcome"
    elif outcome in {TaskOutcome.BLOCKED, TaskOutcome.QUESTION}:
        status = GraphTransitionStatus.PAUSED
        reason = "human outcome pauses through the existing handoff boundary"
    else:
        status = GraphTransitionStatus.ADVANCED
        reason = f"selected declared edge for outcome {outcome.value}"
    return GraphTransition(
        execution_id=execution_id,
        task_id=task_id,
        workflow_id=definition.workflow_id,
        revision=definition.revision,
        node_id=node_id,
        step=step,
        attempt=attempt,
        outcome=outcome,
        status=status,
        selected_edge=selected_edge,
        reason=reason,
        evidence=result.evidence,
        created_at=created_at,
        question=result.question,
        required_action=result.required_action,
    )


def graph_replay_id(
    execution_id: str,
    task_id: str,
    workflow_id: str,
    revision: int,
    node_id: str,
    step: int,
    attempt: int,
) -> str:
    values = [execution_id, task_id, workflow_id, revision, node_id, step, attempt]
    return sha256(
        json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _node_for(definition: GraphDefinition, node_id: str) -> GraphNode:
    nodes = tuple(node for node in definition.nodes if node.node_id == node_id)
    if len(nodes) != 1:
        raise GraphExecutionError(f"unknown graph node: {node_id}")
    return nodes[0]


def _validate_execution_bounds(
    definition: GraphDefinition,
    *,
    step: int,
    attempt: int,
    started_at: datetime,
    now: datetime,
    policy: GraphExecutionPolicy,
) -> None:
    if type(step) is not int or step <= 0:
        raise GraphExecutionError("graph step must be positive")
    if type(attempt) is not int or attempt <= 0:
        raise GraphExecutionError("graph attempt must be positive")
    if step > max(1, min(policy.max_steps, definition.limits.max_loops)):
        raise GraphExecutionError("graph node-step limit reached")
    if attempt > definition.limits.max_retries + 1:
        raise GraphExecutionError("graph retry limit reached")
    if started_at.tzinfo is None or now.tzinfo is None:
        raise GraphExecutionError("execution timestamps must include a timezone")
    if started_at > now:
        raise GraphExecutionError("execution start cannot be in the future")
    elapsed = (now - started_at).total_seconds()
    timeout = definition.limits.timeout_seconds
    if policy.node_timeout_seconds is not None:
        timeout = min(timeout, policy.node_timeout_seconds)
    if elapsed > timeout:
        raise GraphExecutionError("graph node deadline exceeded")


def _validate_result(result: object, contract: TaskContract) -> TaskResult:
    if isinstance(result, Mapping):
        return TaskResult.from_payload(cast(Mapping[str, object], result), contract)
    if isinstance(result, TaskResult):
        return result.validated(contract)
    raise GraphExecutionError("executor must return a TaskResult or result object")


def _contract_for_node(contract: TaskContract, node: GraphNode) -> TaskContract:
    inputs = dict(contract.inputs)
    inputs["graph_node"] = {
        "node_id": node.node_id,
        "kind": node.kind.value,
        "reference": node.reference.as_dict(),
        "metadata": node.metadata,
    }
    return TaskContract(
        contract.contract_id,
        contract.version,
        contract.step_id,
        inputs,
        contract.capabilities,
        contract.required_artifacts,
        contract.allowed_outcomes,
        contract.required_evidence,
    )


def _edge_matches(
    edge: GraphEdge, outcome: TaskOutcome, evidence: Mapping[str, object]
) -> bool:
    if edge.condition == "always":
        return True
    if edge.condition in {task_outcome.value for task_outcome in TaskOutcome}:
        return edge.condition == outcome.value
    return evidence.get(edge.condition) is True


def _validate_text(value: object, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise GraphExecutionError(f"{label} is required")
    if len(value) > MAX_GRAPH_EXECUTION_TEXT:
        raise GraphExecutionError(
            f"{label} exceeds {MAX_GRAPH_EXECUTION_TEXT} characters"
        )


def _freeze_evidence(value: Mapping[str, object]) -> Mapping[str, object]:
    if not isinstance(cast(object, value), Mapping):
        raise GraphExecutionError("transition evidence must be an object")
    try:
        bounded = _bounded_value(value)
    except ContractError as error:
        raise GraphExecutionError(f"transition evidence is invalid: {error}") from error
    if not isinstance(bounded, dict):
        raise GraphExecutionError("transition evidence must be an object")
    _reject_sensitive_evidence(cast(dict[str, object], bounded))
    normalized = _thaw_evidence(cast(Mapping[str, object], bounded))
    encoded = json.dumps(normalized, sort_keys=True)
    if len(encoded) > MAX_GRAPH_EXECUTION_TEXT * 16:
        raise GraphExecutionError("transition evidence is too large")
    return MappingProxyType(normalized)


def _reject_sensitive_evidence(value: object) -> None:
    if not isinstance(value, Mapping):
        return
    for key, item in cast(Mapping[object, object], value).items():
        normalized_key = re.sub(r"[_-]", "", str(key)).lower()
        if re.search(
            r"(?:token|secret|password|credential|apikey|privatekey)",
            normalized_key,
        ):
            raise GraphExecutionError(
                "transition evidence must not contain credentials"
            )
        if isinstance(item, Mapping):
            _reject_sensitive_evidence(cast(Mapping[str, object], item))
        elif isinstance(item, (list, tuple)):
            for nested in cast(Sequence[object], item):
                _reject_sensitive_evidence(nested)


def _thaw_evidence(value: Mapping[str, object]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, item in value.items():
        if isinstance(item, Mapping):
            result[str(key)] = _thaw_evidence(cast(Mapping[str, object], item))
        elif isinstance(item, (list, tuple)):
            items = cast(Sequence[object], item)
            result[str(key)] = [_thaw_value(entry) for entry in items]
        else:
            result[str(key)] = item
    return result


def _thaw_value(value: object) -> object:
    if isinstance(value, Mapping):
        return _thaw_evidence(cast(Mapping[str, object], value))
    if isinstance(value, (list, tuple)):
        items = cast(Sequence[object], value)
        return [_thaw_value(item) for item in items]
    return value


__all__ = [
    "DEFAULT_GRAPH_MAX_STEPS",
    "GraphExecutionError",
    "GraphExecutionPolicy",
    "GraphExecutionService",
    "GraphNodeExecutor",
    "GraphTransition",
    "GraphTransitionStatus",
    "GraphTransitionStore",
    "MAX_GRAPH_EXECUTION_TEXT",
    "MAX_GRAPH_MAX_STEPS",
    "evaluate_graph_transition",
    "graph_replay_id",
]
