from __future__ import annotations

import time

import pytest

from beehaiive.api.app import create_app
from beehaiive.api.helpers.dashboard import _dashboard_state
from beehaiive.autonomous import (
    AUTONOMOUS_STEPS,
    DEFAULT_AUTOMATION_WORKFLOW_ID,
    AutonomousLifecycleService,
    PlaceholderSkillExecutor,
    bootstrap_automation_workflow,
)
from beehaiive.graph import (
    GraphDefinition,
    GraphEdge,
    GraphNode,
    GraphNodeKind,
    GraphReference,
)
from beehaiive.graph_safety import GraphSafetyService
from beehaiive.orchestrator import Orchestrator
from beehaiive.storage import OrchestratorStore
from tests.conftest import FakeProvider
from tests.support.dashboard.helpers import dashboard_snapshot


def test_bootstrap_persists_and_activates_one_idempotent_workflow(tmp_path) -> None:
    store = OrchestratorStore(tmp_path / "state.sqlite3")

    try:
        definition = bootstrap_automation_workflow(store)
        bootstrap_automation_workflow(store)

        assert definition.workflow_id == DEFAULT_AUTOMATION_WORKFLOW_ID
        assert {node.node_id for node in definition.nodes} == {
            step.name for step in AUTONOMOUS_STEPS
        }
        assert {
            (edge.source, edge.target, edge.condition) for edge in definition.edges
        } == {
            (source.name, target.name, "pass")
            for source, target in zip(
                AUTONOMOUS_STEPS, AUTONOMOUS_STEPS[1:], strict=False
            )
        }
        assert len(store.graph_definitions_for(DEFAULT_AUTOMATION_WORKFLOW_ID)) == 1
        assert store.graph_safety_evidence_for(DEFAULT_AUTOMATION_WORKFLOW_ID, 1)
        assert store.graph_safety_review_for(DEFAULT_AUTOMATION_WORKFLOW_ID, 1)
        active = store.active_graph_version(DEFAULT_AUTOMATION_WORKFLOW_ID)
        assert active is not None
        assert active["workflow_id"] == DEFAULT_AUTOMATION_WORKFLOW_ID
        assert active["revision"] == 1
    finally:
        store.close()


def test_application_startup_reuses_one_workflow_revision_after_reopen(
    tmp_path,
) -> None:
    database = tmp_path / "runtime.sqlite3"
    first = OrchestratorStore(database)
    create_app(store=first)
    first.close()

    second = OrchestratorStore(database)
    create_app(store=second)

    try:
        assert len(second.graph_definitions_for(DEFAULT_AUTOMATION_WORKFLOW_ID)) == 1
        active = second.active_graph_version(DEFAULT_AUTOMATION_WORKFLOW_ID)
        assert active is not None
        assert active["revision"] == 1
    finally:
        second.close()


def test_external_orchestrator_startup_bootstraps_the_workflow() -> None:
    store = OrchestratorStore()
    orchestrator = Orchestrator(store, FakeProvider(dashboard_snapshot()))

    create_app(orchestrator=orchestrator)

    try:
        assert DEFAULT_AUTOMATION_WORKFLOW_ID in store.graph_workflow_ids()
        assert store.active_graph_version(DEFAULT_AUTOMATION_WORKFLOW_ID) is not None
    finally:
        store.close()


def test_dashboard_defaults_to_the_active_automation_workflow() -> None:
    store = OrchestratorStore()
    orchestrator = Orchestrator(store, FakeProvider(dashboard_snapshot()))
    bootstrap_automation_workflow(store)

    try:
        state = _dashboard_state(orchestrator, "project-1", 100)
        assert state["workflow_ids"] == [DEFAULT_AUTOMATION_WORKFLOW_ID]
        assert state["graph"]["workflow_id"] == DEFAULT_AUTOMATION_WORKFLOW_ID
        assert state["graph"]["active"]["revision"] == 1
    finally:
        store.close()


def test_autonomous_run_uses_the_active_workflow_and_records_its_identity() -> None:
    store = OrchestratorStore()
    orchestrator = Orchestrator(store, FakeProvider(dashboard_snapshot()))
    orchestrator.synchronize("project-1")
    bootstrap_automation_workflow(store)
    service = AutonomousLifecycleService(orchestrator, PlaceholderSkillExecutor())

    try:
        started = service.start(
            "project-1",
            repository="owner/api",
            pbi_number=1,
            workflow_id=DEFAULT_AUTOMATION_WORKFLOW_ID,
        )
        deadline = time.monotonic() + 3
        current = service.status(str(started["run_id"]))
        while current["status"] == "running" and time.monotonic() < deadline:
            time.sleep(0.01)
            current = service.status(str(started["run_id"]))

        assert current["status"] == "completed"
        assert current["workflow_id"] == DEFAULT_AUTOMATION_WORKFLOW_ID
        assert [item["step"] for item in current["handoffs"]] == [
            step.name for step in AUTONOMOUS_STEPS
        ]
        action = next(
            item
            for item in store.actions_for_project("project-1")
            if item["kind"] == "autonomous_start"
        )
        assert action["request"]["workflow_id"] == DEFAULT_AUTOMATION_WORKFLOW_ID
    finally:
        store.close()


def test_autonomous_run_rejects_an_unavailable_workflow_before_claiming() -> None:
    store = OrchestratorStore()
    orchestrator = Orchestrator(store, FakeProvider(dashboard_snapshot()))
    orchestrator.synchronize("project-1")
    service = AutonomousLifecycleService(orchestrator, PlaceholderSkillExecutor())

    try:
        with pytest.raises(ValueError, match="active revision"):
            service.start(
                "project-1",
                repository="owner/api",
                pbi_number=1,
                workflow_id="missing-workflow",
            )
        assert not any(
            item["kind"] == "autonomous_start"
            for item in store.actions_for_project("project-1")
        )
    finally:
        store.close()


def test_permuted_active_workflow_drives_run_and_resume_order() -> None:
    store = OrchestratorStore()
    orchestrator = Orchestrator(store, FakeProvider(dashboard_snapshot()))
    orchestrator.synchronize("project-1")
    ordered_steps = (
        AUTONOMOUS_STEPS[2],
        AUTONOMOUS_STEPS[0],
        AUTONOMOUS_STEPS[3],
        AUTONOMOUS_STEPS[1],
        AUTONOMOUS_STEPS[5],
        AUTONOMOUS_STEPS[4],
    )
    definition = GraphDefinition(
        "permuted-flow",
        1,
        tuple(
            GraphNode(
                step.name,
                GraphNodeKind.SKILL,
                GraphReference(f"skill/{step.name}"),
            )
            for step in ordered_steps
        ),
        tuple(
            GraphEdge(source.name, target.name, "pass")
            for source, target in zip(ordered_steps, ordered_steps[1:], strict=False)
        ),
    )
    fixtures = {
        "happy": {
            node.node_id: {
                "outcome": "pass",
                "evidence": {},
                "artifact_refs": [],
                "question": None,
                "required_action": None,
                "validation_reason": None,
                "answer": None,
            }
            for node in definition.nodes
        }
    }
    safety = GraphSafetyService(store)
    evaluation = safety.evaluate(definition, fixtures)
    safety.review(evaluation, "operator")
    safety.activate(evaluation, "operator")
    service = AutonomousLifecycleService(orchestrator, PlaceholderSkillExecutor())

    try:
        started = service.start(
            "project-1",
            repository="owner/api",
            pbi_number=1,
            workflow_id="permuted-flow",
        )
        deadline = time.monotonic() + 3
        current = service.status(str(started["run_id"]))
        while current["status"] == "running" and time.monotonic() < deadline:
            time.sleep(0.01)
            current = service.status(str(started["run_id"]))
        assert current["status"] == "completed"
        assert [item["step"] for item in current["handoffs"]] == [
            step.name for step in ordered_steps
        ]

        actions = [
            {
                "kind": "autonomous_start",
                "status": "succeeded",
                "repository": "owner/api",
                "pbi_number": 1,
                "request": {"workflow_id": "permuted-flow"},
            },
            {
                "kind": "skill:submit-draft-pr",
                "status": "succeeded",
                "repository": "owner/api",
                "pbi_number": 1,
                "result": {"handover": {"branch": "codex/permuted"}},
            },
        ]
        resume = AutonomousLifecycleService._resume_context(
            actions,
            "owner/api",
            1,
            tuple(step.name for step in ordered_steps),
        )
        assert resume["workflow_id"] == "permuted-flow"
        assert resume["resume_step"] == "refine-backlog-item"
    finally:
        store.close()
