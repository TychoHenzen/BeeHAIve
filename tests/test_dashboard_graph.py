from __future__ import annotations

import json
import json as json_module
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

import main as main_module
from beehaiive import (
    GraphDefinition,
    GraphEdge,
    GraphNode,
    GraphNodeKind,
    GraphReference,
    GraphSafetyService,
    GraphTransition,
    GraphTransitionStatus,
    Orchestrator,
    OrchestratorStore,
    TaskOutcome,
)
from beehaiive.api.helpers.dashboard import _dashboard_graph_state
from beehaiive.dashboard.values import safe_dashboard_value
from tests.conftest import FakeProvider
from tests.support.dashboard.helpers import dashboard_snapshot


def _definition(revision: int = 1) -> GraphDefinition:
    return GraphDefinition(
        "dashboard-flow",
        revision,
        (
            GraphNode("start", GraphNodeKind.PROMPT, GraphReference("prompt/start")),
            GraphNode("done", GraphNodeKind.SKILL, GraphReference("skill/done")),
        ),
        (GraphEdge("start", "done", "pass"),),
        metadata={"revision": revision},
    )


def _fixtures(*, secret: bool = False) -> dict[str, dict[str, object]]:
    result = {
        "outcome": "pass",
        "evidence": (
            {"message": "test-key", "safe=test-key": "test-key"} if secret else {}
        ),
        "artifact_refs": [],
        "question": None,
        "required_action": None,
        "validation_reason": None,
        "answer": None,
    }
    return {"happy": {"start": result, "done": result}}


def _client(
    service: Orchestrator, graph_safety_service: GraphSafetyService | None = None
) -> TestClient:
    return TestClient(
        main_module.create_app(
            orchestrator=service,
            api_key="test-key",
            allowed_project_ids={"project-1"},
            workflow_actor="operator",
            graph_safety_service=graph_safety_service,
        )
    )


def test_dashboard_graph_state_and_actions_use_existing_safety_service() -> None:
    service = Orchestrator(OrchestratorStore(), FakeProvider(dashboard_snapshot()))
    GraphSafetyService(service.store).evaluate(_definition(), _fixtures())
    client = _client(service)
    headers = {"X-API-Key": "test-key"}
    state = client.get("/projects/project-1/dashboard?workflow_id=dashboard-flow")
    assert state.status_code == 200
    graph = state.json()["graph"]
    assert graph["workflow_id"] == "dashboard-flow"
    assert graph["definitions"][0]["nodes"][0]["node_id"] == "done"
    assert graph["active"] is None

    payload = {
        "action": "graph_evaluate",
        "approved": True,
        "workflow_id": "dashboard-flow",
        "candidate": _definition().as_dict(),
        "fixtures": _fixtures(),
    }
    evaluated = client.post(
        "/projects/project-1/actions", headers=headers, json=payload
    )
    assert evaluated.status_code == 200
    assert evaluated.json()["result"]["graph"]["activatable"] is True

    reviewed = client.post(
        "/projects/project-1/actions",
        headers=headers,
        json={**payload, "action": "graph_review"},
    )
    assert reviewed.status_code == 200
    assert reviewed.json()["result"]["review"]["actor"] == "operator"

    activated = client.post(
        "/projects/project-1/actions",
        headers=headers,
        json={**payload, "action": "graph_activate"},
    )
    assert activated.status_code == 200
    assert activated.json()["result"]["activation"]["revision"] == 1
    active = client.get("/projects/project-1/dashboard?workflow_id=dashboard-flow")
    assert active.json()["graph"]["active"]["revision"] == 1
    assert active.json()["workflow_ids"] == ["dashboard-flow"]
    service.store.close()


def test_dashboard_query_validation_does_not_echo_secrets() -> None:
    service = Orchestrator(OrchestratorStore(), FakeProvider(dashboard_snapshot()))
    response = _client(service).get(
        "/projects/project-1/dashboard?workflow_id=" + ("test-key" * 30)
    )
    assert response.status_code == 422
    assert "test-key" not in response.text
    service.store.close()


def test_dashboard_launcher_loads_workflow_operator_configuration() -> None:
    launcher = Path("start_dashboard.bat").read_text(encoding="utf-8")
    assert '"BEEHAIIVE_WORKFLOW_ACTOR"' in launcher
    for name in (
        "BEEHAIIVE_AUTONOMOUS_MODE",
        "BEEHAIIVE_AUTONOMOUS_TIMEOUT_SECONDS",
        "BEEHAIIVE_SCHEDULER_ENABLED",
        "BEEHAIIVE_SCHEDULER_POLL_INTERVAL_SECONDS",
        "BEEHAIIVE_SCHEDULER_MAX_CONCURRENCY",
        "BEEHAIIVE_STATE_DB",
        "BEEHAIIVE_WORKFLOW_DB",
    ):
        assert f'"{name}"' in launcher


def test_graph_actions_require_workflow_operator_for_mutation() -> None:
    service = Orchestrator(OrchestratorStore(), FakeProvider(dashboard_snapshot()))
    client = TestClient(
        main_module.create_app(
            orchestrator=service,
            api_key="test-key",
            allowed_project_ids={"project-1"},
        )
    )
    response = client.post(
        "/projects/project-1/actions",
        headers={"X-API-Key": "test-key"},
        json={
            "action": "graph_activate",
            "approved": True,
            "workflow_id": "dashboard-flow",
            "candidate": _definition().as_dict(),
            "fixtures": _fixtures(),
        },
    )
    assert response.status_code == 503
    assert "Workflow operator" in response.json()["detail"]
    service.store.close()


def test_dashboard_activates_a_revision_with_explicit_baseline() -> None:
    service = Orchestrator(OrchestratorStore(), FakeProvider(dashboard_snapshot()))
    client = _client(service)
    headers = {"X-API-Key": "test-key"}
    first = {
        "action": "graph_evaluate",
        "approved": True,
        "workflow_id": "dashboard-flow",
        "candidate": _definition(1).as_dict(),
        "fixtures": _fixtures(),
    }
    assert (
        client.post(
            "/projects/project-1/actions", headers=headers, json=first
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/projects/project-1/actions",
            headers=headers,
            json={**first, "action": "graph_review"},
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/projects/project-1/actions",
            headers=headers,
            json={**first, "action": "graph_activate"},
        ).status_code
        == 200
    )

    second = {
        **first,
        "candidate": _definition(2).as_dict(),
        "baseline": _definition(1).as_dict(),
        "baseline_fixtures": _fixtures(),
    }
    assert (
        client.post(
            "/projects/project-1/actions",
            headers=headers,
            json={**second, "action": "graph_review"},
        ).status_code
        == 200
    )
    activated = client.post(
        "/projects/project-1/actions",
        headers=headers,
        json={**second, "action": "graph_activate"},
    )
    assert activated.status_code == 200
    assert activated.json()["result"]["activation"]["revision"] == 2
    rolled_back = client.post(
        "/projects/project-1/actions",
        headers=headers,
        json={
            "action": "graph_rollback",
            "approved": True,
            "workflow_id": "dashboard-flow",
            "revision": 1,
        },
    )
    assert rolled_back.status_code == 200
    assert rolled_back.json()["result"]["graph"]["activation"]["revision"] == 1
    service.store.close()


def test_graph_action_redacts_secrets_before_safety_persistence() -> None:
    service = Orchestrator(OrchestratorStore(), FakeProvider(dashboard_snapshot()))
    client = _client(service)
    candidate = _definition().as_dict()
    candidate["api_key=test-key"] = "test-key"
    response = client.post(
        "/projects/project-1/actions",
        headers={"X-API-Key": "test-key"},
        json={
            "action": "graph_evaluate",
            "approved": True,
            "workflow_id": "dashboard-flow",
            "candidate": candidate,
            "fixtures": _fixtures(secret=True),
        },
    )
    assert response.status_code == 200
    assert "test-key" not in repr(response.json())
    evidence = service.store.graph_safety_evidence_for("dashboard-flow", 1)
    assert evidence is not None
    assert "test-key" not in repr(evidence)
    service.store.close()


def test_dashboard_uses_the_configured_graph_safety_service() -> None:
    service = Orchestrator(OrchestratorStore(), FakeProvider(dashboard_snapshot()))

    class RecordingGraphSafetyService(GraphSafetyService):
        calls = 0

        def evaluate(self, *args, **kwargs):
            self.calls += 1
            return super().evaluate(*args, **kwargs)

    graph_service = RecordingGraphSafetyService(service.store)
    response = _client(service, graph_service).post(
        "/projects/project-1/actions",
        headers={"X-API-Key": "test-key"},
        json={
            "action": "graph_evaluate",
            "approved": True,
            "workflow_id": "dashboard-flow",
            "candidate": _definition().as_dict(),
            "fixtures": _fixtures(),
        },
    )
    assert response.status_code == 200
    assert graph_service.calls == 1
    service.store.close()


def test_non_graph_action_keeps_the_selected_workflow_in_readback() -> None:
    service = Orchestrator(OrchestratorStore(), FakeProvider(dashboard_snapshot()))
    GraphSafetyService(service.store).evaluate(_definition(), _fixtures())
    client = _client(service)
    response = client.post(
        "/projects/project-1/actions?workflow_id=dashboard-flow",
        headers={"X-API-Key": "test-key"},
        json={"action": "synchronize", "approved": True},
    )
    assert response.status_code == 200
    assert response.json()["state"]["graph"]["workflow_id"] == "dashboard-flow"
    service.store.close()


def test_graph_action_rejects_selected_workflow_mismatch() -> None:
    service = Orchestrator(OrchestratorStore(), FakeProvider(dashboard_snapshot()))
    client = _client(service)
    response = client.post(
        "/projects/project-1/actions",
        headers={"X-API-Key": "test-key"},
        json={
            "action": "graph_evaluate",
            "approved": True,
            "workflow_id": "selected-flow",
            "candidate": _definition().as_dict(),
            "fixtures": _fixtures(),
        },
    )
    assert response.status_code == 200
    assert response.json()["action"]["status"] == "failed"
    assert "workflow ID" in response.json()["action"]["error"]
    assert service.store.graph_definition_for("dashboard-flow", 1) is None
    service.store.close()


def test_dashboard_graph_trace_stays_within_aggregate_byte_bound() -> None:
    service = Orchestrator(OrchestratorStore(), FakeProvider(dashboard_snapshot()))
    service.synchronize("project-1")
    run = service.claim("project-1", "owner/api", "worker")
    assert run is not None
    for step in range(1, 31):
        service.store.record_graph_transition(
            GraphTransition(
                execution_id=run.run_id,
                task_id=f"task-{step}",
                workflow_id="dashboard-flow",
                revision=1,
                node_id="start",
                step=step,
                attempt=1,
                outcome=TaskOutcome.PASS,
                status=GraphTransitionStatus.TERMINAL,
                selected_edge=None,
                reason="finished",
                evidence={"text": "x" * 4_000},
                created_at=f"2026-09-16T00:00:{step:02d}+00:00",
            )
        )
    response = _client(service).get("/projects/project-1/dashboard")
    assert response.status_code == 200
    trace = response.json()["repositories"][0]["pbis"][0]["graph_trace"]
    assert len(json.dumps(trace, separators=(",", ":")).encode("utf-8")) <= 64_000
    assert trace[-1]["step"] == 30
    service.store.close()


def test_dashboard_graph_trace_bounds_each_event() -> None:
    service = Orchestrator(OrchestratorStore(), FakeProvider(dashboard_snapshot()))
    service.synchronize("project-1")
    run = service.claim("project-1", "owner/api", "worker")
    assert run is not None
    service.store.record_graph_transition(
        GraphTransition(
            execution_id=run.run_id,
            task_id="task-large",
            workflow_id="dashboard-flow",
            revision=1,
            node_id="start",
            step=1,
            attempt=1,
            outcome=TaskOutcome.PASS,
            status=GraphTransitionStatus.TERMINAL,
            selected_edge=None,
            reason="finished",
            evidence={"text": "x" * 4_000},
            created_at="2026-09-16T00:00:00+00:00",
        )
    )
    trace = service.store.project_state("project-1")["repositories"][0]["pbis"][0][
        "graph_trace"
    ]
    assert len(json.dumps(trace[0], ensure_ascii=False, separators=(",", ":"))) <= 4_000
    assert trace[0]["evidence"] == {"truncated": True}
    service.store.close()


def test_dashboard_graph_trace_bound_survives_redaction() -> None:
    event = {
        "execution_id": "execution",
        "task_id": "task",
        "workflow_id": "dashboard-flow",
        "revision": 1,
        "node_id": "start",
        "step": 1,
        "attempt": 1,
        "outcome": "pass",
        "status": "terminal",
        "selected_edge": None,
        "reason": "finished",
        "evidence": {"text": "x" * 3_900, "token": "test-key"},
        "created_at": "2026-09-16T00:00:00+00:00",
        "question": None,
        "required_action": None,
        "replay_id": "replay",
    }
    projected = safe_dashboard_value([event], ("test-key",), max_bytes=64_000)
    assert (
        len(json.dumps(projected[0], ensure_ascii=False, separators=(",", ":")))
        <= 4_000
    )
    assert projected[0]["evidence"] == {"truncated": True}


def test_dashboard_graph_state_keeps_an_old_active_definition() -> None:
    class GraphReadStore:
        def graph_definitions_for(self, workflow_id: str):
            return tuple(_definition(revision) for revision in range(1, 102))

        def active_graph_version(self, workflow_id: str):
            return {"workflow_id": workflow_id, "revision": 1}

        def graph_safety_evidence_for(self, workflow_id: str, revision: int):
            return None

        def graph_safety_review_for(self, workflow_id: str, revision: int):
            return None

    graph = _dashboard_graph_state(
        SimpleNamespace(store=GraphReadStore()),
        "dashboard-flow",
        GraphSafetyService(GraphReadStore()),
    )
    revisions = [definition["revision"] for definition in graph["definitions"]]
    assert len(revisions) == 100
    assert revisions[0] == 1
    assert graph["definitions"][0]["active"] is True


def test_dashboard_graph_projection_keeps_128_edges() -> None:
    projected = safe_dashboard_value(
        {
            "graph": {
                "edges": [
                    {"source": "a", "target": "b", "condition": str(i)}
                    for i in range(128)
                ]
            }
        }
    )
    assert len(projected["graph"]["edges"]) == 128


def test_dashboard_redacts_configured_secrets_used_as_non_structural_keys() -> None:
    projected = safe_dashboard_value(
        {"test-key": "value", "result": "ok"}, ("test-key",)
    )
    assert "test-key" not in repr(projected)
    assert "result" in projected


def test_dashboard_projects_graph_transitions_into_bounded_trace() -> None:
    service = Orchestrator(OrchestratorStore(), FakeProvider(dashboard_snapshot()))
    service.synchronize("project-1")
    run = service.claim("project-1", "owner/api", "worker")
    assert run is not None
    service.store.record_graph_transition(
        GraphTransition(
            execution_id=run.run_id,
            task_id="task-1",
            workflow_id="dashboard-flow",
            revision=1,
            node_id="start",
            step=1,
            attempt=1,
            outcome=TaskOutcome.PASS,
            status=GraphTransitionStatus.TERMINAL,
            selected_edge=None,
            reason="finished",
            evidence={"summary": "safe"},
            created_at="2026-09-16T00:00:00+00:00",
        )
    )
    client = _client(service)
    response = client.get("/projects/project-1/dashboard")
    assert response.status_code == 200
    trace = response.json()["repositories"][0]["pbis"][0]["graph_trace"]
    assert trace[0]["node_id"] == "start"
    assert trace[0]["outcome"] == "pass"
    service.store.close()


def test_raw_project_state_omits_graph_trace_but_dashboard_includes_it() -> None:
    service = Orchestrator(OrchestratorStore(), FakeProvider(dashboard_snapshot()))
    service.synchronize("project-1")
    run = service.claim("project-1", "owner/api", "worker")
    assert run is not None
    service.store.record_graph_transition(
        GraphTransition(
            execution_id=run.run_id,
            task_id="task-raw",
            workflow_id="dashboard-flow",
            revision=1,
            node_id="start",
            step=1,
            attempt=1,
            outcome=TaskOutcome.PASS,
            status=GraphTransitionStatus.TERMINAL,
            selected_edge=None,
            reason="finished",
            evidence={"summary": "safe"},
            created_at="2026-09-16T00:00:00+00:00",
        )
    )
    client = _client(service)
    raw = client.get("/projects/project-1")
    dashboard = client.get("/projects/project-1/dashboard")
    assert raw.status_code == 200
    assert "graph_trace" not in repr(raw.json())
    assert dashboard.status_code == 200
    assert dashboard.json()["repositories"][0]["pbis"][0]["graph_trace"]
    service.store.close()


def test_oversized_graph_action_is_rejected_before_action_persistence() -> None:
    service = Orchestrator(OrchestratorStore(), FakeProvider(dashboard_snapshot()))
    client = _client(service)
    candidate = _definition().as_dict()
    candidate.update({f"extra_{index}": "x" * 1_000 for index in range(100)})
    response = client.post(
        "/projects/project-1/actions",
        headers={"X-API-Key": "test-key"},
        json={
            "action": "graph_evaluate",
            "approved": True,
            "workflow_id": "dashboard-flow",
            "candidate": candidate,
            "fixtures": _fixtures(),
        },
    )
    assert response.status_code == 413
    assert service.store.actions_for_project("project-1") == []
    service.store.close()


def test_raw_whitespace_action_body_is_rejected_before_model_normalization() -> None:
    service = Orchestrator(OrchestratorStore(), FakeProvider(dashboard_snapshot()))
    client = _client(service)
    body = json_module.dumps({"action": "synchronize", "approved": True})
    response = client.post(
        "/projects/project-1/actions",
        headers={"X-API-Key": "test-key", "Content-Type": "application/json"},
        content=(body + (" " * 64_000)).encode("utf-8"),
    )
    assert response.status_code == 413
    assert service.store.actions_for_project("project-1") == []
    service.store.close()


def test_many_small_graph_keys_are_rejected_before_sanitization() -> None:
    service = Orchestrator(OrchestratorStore(), FakeProvider(dashboard_snapshot()))
    client = _client(service)
    candidate = _definition().as_dict()
    candidate.update({f"extra_{index}": "x" * 100 for index in range(1_000)})
    response = client.post(
        "/projects/project-1/actions",
        headers={"X-API-Key": "test-key"},
        json={
            "action": "graph_evaluate",
            "approved": True,
            "workflow_id": "dashboard-flow",
            "candidate": candidate,
            "fixtures": _fixtures(),
        },
    )
    assert response.status_code == 413
    assert service.store.actions_for_project("project-1") == []
    service.store.close()
