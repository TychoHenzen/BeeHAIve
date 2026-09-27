from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from beehaiive.agent_stations import AgentStationService
from beehaiive.models import PbiSnapshot, ProjectSnapshot, RepositorySnapshot, Stage
from beehaiive.orchestrator import Orchestrator
from beehaiive.persistence import OrchestratorStore
from main import create_app
from tests.conftest import FakeProvider


def _service(tmp_path: Path) -> tuple[Orchestrator, OrchestratorStore]:
    store = OrchestratorStore(tmp_path / "stations.sqlite3")
    snapshot = ProjectSnapshot(
        "project-1",
        "Station test",
        (
            RepositorySnapshot(
                "owner/api",
                (
                    PbiSnapshot(
                        "owner/api",
                        1,
                        "Implement station view",
                        stage=Stage.IMPLEMENT,
                        planning_status="In Progress",
                        metadata={"labels": ["Effort 5 - Large"]},
                    ),
                ),
            ),
        ),
    )
    orchestrator = Orchestrator(store, FakeProvider(snapshot))
    orchestrator.synchronize("project-1")
    return orchestrator, store


def test_station_projection_maps_active_run_and_restarts_with_durable_issue(
    tmp_path: Path,
) -> None:
    orchestrator, store = _service(tmp_path)
    run = orchestrator.claim(
        "project-1",
        "owner/api",
        "worker-1",
        agent_session=("agent-1", "implement station view"),
    )
    assert run is not None
    lease = run.lease_token or ""
    store.record_agent_session_event(
        run.run_id, lease, "message", "item.completed", "assistant", "Started safely"
    )
    active_view = AgentStationService(store).view("project-1")
    active_agent = active_view["agents"][0]
    assert active_agent["station_id"] == "smelting"
    assert active_agent["estimate"]["status"] == "available"
    assert active_agent["estimate"]["duration_seconds"] == 4_500
    failed = store.fail_agent_run(
        run.run_id, "secret-token=do-not-leak", lease, claimable=True
    )
    assert failed.status.value == "failed"

    service = AgentStationService(store)
    view = service.view("project-1")
    assert view["agents"] == []
    assert "do-not-leak" not in str(view)
    assert view["issues"][0]["status"] == "open"

    store.close()
    reopened = OrchestratorStore(tmp_path / "stations.sqlite3")
    persisted = AgentStationService(reopened).view("project-1")
    assert persisted["issues"][0]["id"] == view["issues"][0]["id"]
    addressed = AgentStationService(reopened).act_on_issue(
        "project-1",
        persisted["issues"][0]["id"],
        "address",
        "operator",
        "Investigating",
    )
    assert addressed["issue"]["status"] == "addressed"
    resolved = AgentStationService(reopened).act_on_issue(
        "project-1", persisted["issues"][0]["id"], "resolve", "operator", "Fixed"
    )
    assert resolved["issue"]["status"] == "resolved"
    assert len(resolved["issue"]["attempts"]) == 2
    reopened.close()


def test_station_api_is_scoped_redacted_and_operator_actions_are_authenticated(
    tmp_path: Path,
) -> None:
    orchestrator, store = _service(tmp_path)
    issue = store.ensure_station_issue(
        "station-manual",
        "project-1",
        "owner/api",
        1,
        "run-1",
        "smelting",
        "Needs operator review",
    )
    client = TestClient(
        create_app(
            orchestrator=orchestrator,
            api_key="test-key",
            allowed_project_ids={"project-1"},
            workflow_actor="operator",
        )
    )

    page = client.get("/agent-stations")
    assert page.status_code == 200
    assert 'id="stations"' in page.text
    assert "/agent-stations.js" in page.text
    view = client.get("/projects/project-1/agent-stations")
    assert view.status_code == 200
    assert view.json()["issues"][0]["id"] == issue["id"]
    assert client.get("/projects/other/agent-stations").status_code == 403
    assert (
        client.post(
            "/projects/project-1/agent-stations/issues/station-manual/address",
            json={"note": "unauthorized"},
        ).status_code
        == 401
    )
    changed = client.post(
        "/projects/project-1/agent-stations/issues/station-manual/address",
        json={"note": "Operator checked"},
        headers={"X-API-Key": "test-key"},
    )
    assert changed.status_code == 200
    assert changed.json()["issue"]["status"] == "addressed"
    store.close()


def test_station_estimate_is_unavailable_without_effort_metadata(
    tmp_path: Path,
) -> None:
    orchestrator, store = _service(tmp_path)
    store._connection.execute(
        "UPDATE pbis SET metadata_json = '{}' WHERE project_id = 'project-1'"
    )
    run = orchestrator.claim("project-1", "owner/api", "worker-1")
    assert run is not None
    estimate = AgentStationService(store).view("project-1")["agents"][0]["estimate"]
    assert estimate == {
        "status": "unavailable",
        "reason": "Effort label or phase average is unavailable",
    }
    store.close()
