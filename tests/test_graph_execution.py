from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from beehaiive import (
    GraphDefinition,
    GraphEdge,
    GraphExecutionError,
    GraphExecutionPolicy,
    GraphExecutionService,
    GraphNode,
    GraphNodeKind,
    GraphReference,
    GraphTransition,
    GraphTransitionStatus,
    ModelTier,
    TaskContract,
    TaskOutcome,
    TaskResult,
    evaluate_graph_transition,
)
from beehaiive.storage import OrchestratorStore, StoreError


def _contract() -> TaskContract:
    return TaskContract(
        "graph.task",
        1,
        "node",
        {},
        (),
        (),
        tuple(TaskOutcome),
    )


def _definition(*edges: GraphEdge) -> GraphDefinition:
    return GraphDefinition(
        "flow",
        1,
        (
            GraphNode("start", GraphNodeKind.PROMPT, GraphReference("prompt/start")),
            GraphNode("done", GraphNodeKind.SKILL, GraphReference("skill/done")),
        ),
        edges,
        metadata={"model": ModelTier.LUNA.value},
    )


def _result(outcome: TaskOutcome) -> TaskResult:
    return TaskResult(
        outcome,
        {},
        question="Need input" if outcome is TaskOutcome.QUESTION else None,
        required_action="Stop" if outcome is TaskOutcome.BLOCKED else None,
    )


def test_graph_transition_selects_declared_edge_and_replays(tmp_path: Path) -> None:
    definition = _definition(GraphEdge("start", "done", "pass"))
    transition = evaluate_graph_transition(
        definition,
        execution_id="execution-1",
        task_id="task-1",
        node_id="start",
        result=_result(TaskOutcome.PASS),
        step=1,
        attempt=1,
        created_at=datetime.now(UTC).isoformat(),
    )
    assert transition.status is GraphTransitionStatus.ADVANCED
    assert transition.selected_edge == GraphEdge("start", "done", "pass")
    store = OrchestratorStore(tmp_path / "transitions.sqlite3")
    assert store.record_graph_transition(transition) == transition
    assert store.record_graph_transition(transition) == transition
    assert store.graph_transitions_for("execution-1") == (transition,)
    store.close()


def test_graph_transition_pauses_human_outcomes_and_rejects_ambiguity() -> None:
    paused = evaluate_graph_transition(
        _definition(),
        execution_id="execution-1",
        task_id="task-1",
        node_id="start",
        result=_result(TaskOutcome.QUESTION),
        step=1,
        attempt=1,
        created_at=datetime.now(UTC).isoformat(),
    )
    assert paused.status is GraphTransitionStatus.TERMINAL
    ambiguous = _definition(
        GraphEdge("start", "done", "pass"), GraphEdge("start", "done", "always")
    )
    with pytest.raises(GraphExecutionError, match="multiple"):
        evaluate_graph_transition(
            ambiguous,
            execution_id="execution-1",
            task_id="task-1",
            node_id="start",
            result=_result(TaskOutcome.PASS),
            step=1,
            attempt=1,
            created_at=datetime.now(UTC).isoformat(),
        )
    evidence_edge = evaluate_graph_transition(
        _definition(GraphEdge("start", "done", "approved")),
        execution_id="execution-1",
        task_id="task-1",
        node_id="start",
        result=TaskResult(TaskOutcome.PASS, {"approved": True}),
        step=1,
        attempt=1,
        created_at=datetime.now(UTC).isoformat(),
    )
    assert evidence_edge.status is GraphTransitionStatus.ADVANCED
    with pytest.raises(GraphExecutionError, match="credentials"):
        GraphTransition(
            execution_id="execution-1",
            task_id="task-1",
            workflow_id="flow",
            revision=1,
            node_id="start",
            step=1,
            attempt=1,
            outcome=TaskOutcome.PASS,
            status=GraphTransitionStatus.TERMINAL,
            selected_edge=None,
            reason="done",
            evidence={"token": "secret"},
            created_at=datetime.now(UTC).isoformat(),
        )


def test_graph_service_validates_results_and_execution_bounds(tmp_path: Path) -> None:
    store = OrchestratorStore(tmp_path / "service.sqlite3")
    service = GraphExecutionService(store)
    definition = _definition(GraphEdge("start", "done", "pass"))
    contract = _contract()
    calls = 0

    def execute(_node: GraphNode, _contract: TaskContract) -> TaskResult:
        nonlocal calls
        calls += 1
        return _result(TaskOutcome.PASS)

    transition = service.execute_node(
        definition,
        execution_id="execution-1",
        task_id="task-1",
        node_id="start",
        contract=contract,
        executor=execute,
        step=1,
        attempt=1,
    )
    assert transition.status is GraphTransitionStatus.ADVANCED
    assert (
        service.execute_node(
            definition,
            execution_id="execution-1",
            task_id="task-1",
            node_id="start",
            contract=contract,
            executor=execute,
            step=1,
            attempt=1,
        )
        == transition
    )
    assert calls == 1
    second_task = service.execute_node(
        definition,
        execution_id="execution-1",
        task_id="task-2",
        node_id="start",
        contract=contract,
        executor=execute,
        step=1,
        attempt=1,
    )
    assert second_task.task_id == "task-2"
    assert calls == 2
    with pytest.raises(GraphExecutionError, match="step limit"):
        service.execute_node(
            definition,
            execution_id="execution-1",
            task_id="task-2",
            node_id="start",
            contract=contract,
            executor=lambda _node, _contract: _result(TaskOutcome.PASS),
            step=2,
            attempt=1,
            policy=GraphExecutionPolicy(max_steps=1),
        )
    with pytest.raises(GraphExecutionError, match="step limit"):
        service.execute_node(
            definition,
            execution_id="execution-1",
            task_id="task-3",
            node_id="start",
            contract=contract,
            executor=execute,
            step=9,
            attempt=1,
        )
    with pytest.raises(GraphExecutionError, match="deadline"):
        service.execute_node(
            definition,
            execution_id="execution-2",
            task_id="task-2",
            node_id="start",
            contract=contract,
            executor=lambda _node, _contract: _result(TaskOutcome.PASS),
            step=1,
            attempt=1,
            started_at=datetime.now(UTC) - timedelta(seconds=2),
            policy=GraphExecutionPolicy(node_timeout_seconds=1),
        )
    with pytest.raises(GraphExecutionError, match="invalid"):
        service.execute_node(
            definition,
            execution_id="execution-3",
            task_id="task-3",
            node_id="start",
            contract=contract,
            executor=lambda _node, _contract: {"outcome": "unknown"},  # type: ignore[arg-type]
            step=1,
            attempt=1,
        )
    store.close()


def test_graph_transition_replay_conflict_and_corrupt_storage_fail_closed(
    tmp_path: Path,
) -> None:
    store = OrchestratorStore(tmp_path / "conflict.sqlite3")
    transition = evaluate_graph_transition(
        _definition(GraphEdge("start", "done", "pass")),
        execution_id="execution-1",
        task_id="task-1",
        node_id="start",
        result=_result(TaskOutcome.PASS),
        step=1,
        attempt=1,
        created_at=datetime.now(UTC).isoformat(),
    )
    store.record_graph_transition(transition)
    conflicting = replace(transition, reason="different")
    with pytest.raises(StoreError, match="replay identity"):
        store.record_graph_transition(conflicting)
    store._connection.execute(  # pyright: ignore[reportPrivateUsage]
        "UPDATE graph_transitions SET transition_json = '[]'"
    )
    with pytest.raises(StoreError, match="graph transition is invalid"):
        store.graph_transitions_for("execution-1")
    store.close()


def test_graph_transition_claim_release_is_owner_fenced(tmp_path: Path) -> None:
    store = OrchestratorStore(tmp_path / "claims.sqlite3")
    assert store.claim_graph_transition("replay", "owner-a") is None
    with pytest.raises(StoreError, match="already in progress"):
        store.claim_graph_transition("replay", "owner-b")
    store.release_graph_transition("replay", "owner-b")
    with pytest.raises(StoreError, match="already in progress"):
        store.claim_graph_transition("replay", "owner-b")
    store.release_graph_transition("replay", "owner-a")
    assert store.claim_graph_transition("replay", "owner-b") is None
    store.release_graph_transition("replay", "owner-b")
    store.close()


def test_graph_service_uses_existing_orchestrator_attempt_boundary(
    tmp_path: Path,
) -> None:
    store = OrchestratorStore(tmp_path / "orchestrated.sqlite3")
    service = GraphExecutionService(store)
    calls: list[tuple[str, str]] = []
    options_seen: list[dict[str, object]] = []

    class OrchestratorDouble:
        def run_implementation_attempt(
            self, run_id: str, lease_token: str, **options: object
        ) -> SimpleNamespace:
            calls.append((run_id, lease_token))
            options_seen.append(options)
            return SimpleNamespace(task_result=_result(TaskOutcome.PASS))

    transition = service.execute_orchestrated_node(
        OrchestratorDouble(),
        _definition(GraphEdge("start", "done", "pass")),
        run_id="run-1",
        lease_token="lease-1",
        task_id="task-1",
        node_id="start",
        contract=_contract(),
        step=1,
        attempt=1,
    )
    assert transition.status is GraphTransitionStatus.ADVANCED
    assert calls == [("run-1", "lease-1")]
    assert str(options_seen[0]["routing_problem_id"]).startswith("graph:")
    graph_node = options_seen[0]["task_contract"].inputs["graph_node"]  # type: ignore[attr-defined]
    assert graph_node["node_id"] == "start"  # type: ignore[index]

    class FailedOrchestrator:
        def run_implementation_attempt(
            self, _run_id: str, _lease_token: str, **_options: object
        ) -> SimpleNamespace:
            return SimpleNamespace(task_result=None)

    with pytest.raises(GraphExecutionError, match="validated task result"):
        service.execute_orchestrated_node(
            FailedOrchestrator(),
            _definition(GraphEdge("start", "done", "pass")),
            run_id="run-failed",
            lease_token="lease-failed",
            task_id="task-failed",
            node_id="start",
            contract=_contract(),
            step=1,
            attempt=1,
        )
    assert store.graph_transitions_for("run-failed") == ()
    store.close()


def test_graph_service_persists_human_pause_through_existing_handoff_boundary() -> None:
    class StoreDouble:
        def __init__(self) -> None:
            self.transitions: dict[str, object] = {}
            self.pauses: list[tuple[str, str, str, str]] = []
            self.claims: set[str] = set()
            self.handoff_failures = 1

        def claim_graph_transition(
            self, replay_id: str, owner_id: str
        ) -> object | None:
            existing = self.transitions.get(replay_id)
            if existing is not None:
                return existing
            if replay_id in self.claims:
                raise StoreError("Graph transition is already in progress")
            self.claims.add(replay_id)
            return None

        def release_graph_transition(self, replay_id: str, owner_id: str) -> None:
            self.claims.discard(replay_id)

        def graph_transition_for_replay(self, replay_id: str) -> object | None:
            return self.transitions.get(replay_id)

        def record_graph_transition(
            self, transition: object, *, owner_id: str | None = None
        ) -> object:
            replay_id = cast(str, transition.replay_id)  # type: ignore[attr-defined]
            self.transitions[replay_id] = transition
            self.claims.discard(replay_id)
            return transition

        def await_operator(
            self,
            run_id: str,
            lease_token: str,
            *,
            kind: str,
            question: str,
            evidence: object,
        ) -> None:
            if self.handoff_failures:
                self.handoff_failures -= 1
                raise RuntimeError("temporary handoff failure")
            self.pauses.append((run_id, lease_token, kind, question))

    store = StoreDouble()
    service = GraphExecutionService(store)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="handoff"):
        service.execute_node(
            _definition(GraphEdge("start", "done", "question")),
            execution_id="execution-human",
            task_id="task-human",
            node_id="start",
            contract=_contract(),
            executor=lambda _node, _contract: _result(TaskOutcome.QUESTION),
            step=1,
            attempt=1,
            run_id="run-human",
            lease_token="lease-human",
        )
    transition = service.execute_node(
        _definition(GraphEdge("start", "done", "question")),
        execution_id="execution-human",
        task_id="task-human",
        node_id="start",
        contract=_contract(),
        executor=lambda _node, _contract: _result(TaskOutcome.QUESTION),
        step=1,
        attempt=1,
        run_id="run-human",
        lease_token="lease-human",
    )
    assert transition.status is GraphTransitionStatus.PAUSED
    assert store.pauses == [("run-human", "lease-human", "question", "Need input")]


def test_graph_service_resumes_from_a_persisted_human_edge() -> None:
    class StoreDouble:
        def __init__(self) -> None:
            self.transitions: dict[str, object] = {}
            self.claims: set[str] = set()

        def claim_graph_transition(
            self, replay_id: str, owner_id: str
        ) -> object | None:
            existing = self.transitions.get(replay_id)
            if existing is not None:
                return existing
            if replay_id in self.claims:
                raise StoreError("Graph transition is already in progress")
            self.claims.add(replay_id)
            return None

        def release_graph_transition(self, replay_id: str, owner_id: str) -> None:
            self.claims.discard(replay_id)

        def record_graph_transition(
            self, transition: object, *, owner_id: str | None = None
        ) -> object:
            replay_id = cast(str, transition.replay_id)  # type: ignore[attr-defined]
            self.claims.discard(replay_id)
            self.transitions[replay_id] = transition
            return transition

    store = StoreDouble()
    service = GraphExecutionService(store)  # type: ignore[arg-type]
    definition = _definition(GraphEdge("start", "done", "question"))
    paused = service.execute_node(
        definition,
        execution_id="execution-resume",
        task_id="task-resume",
        node_id="start",
        contract=_contract(),
        executor=lambda _node, _contract: _result(TaskOutcome.QUESTION),
        step=1,
        attempt=1,
    )
    resumed = service.resume_node(
        paused,
        definition,
        contract=_contract(),
        executor=lambda _node, _contract: _result(TaskOutcome.PASS),
    )
    assert paused.status is GraphTransitionStatus.PAUSED
    assert resumed.node_id == "done"
    assert resumed.step == 2
    tampered = replace(paused, selected_edge=GraphEdge("done", "start", "question"))
    with pytest.raises(GraphExecutionError, match="not declared"):
        service.resume_node(
            tampered,
            definition,
            contract=_contract(),
            executor=lambda _node, _contract: _result(TaskOutcome.PASS),
        )
