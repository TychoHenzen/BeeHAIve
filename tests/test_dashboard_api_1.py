from fastapi.testclient import TestClient

import main as main_module
from beehaiive.models import (
    PbiSnapshot,
    ProjectSnapshot,
    RepositorySnapshot,
    Stage,
)
from beehaiive.orchestrator import Orchestrator
from beehaiive.storage import (
    OrchestratorStore,
)
from tests.conftest import FakeProvider
from tests.support.dashboard.helpers import dashboard_snapshot


def test_dashboard_api_exposes_terminal_project_and_pull_request_state() -> None:
    readiness = {
        "status": "completed",
        "counts": {
            "ready": 0,
            "incomplete": 0,
            "blocked": 0,
            "rejected": 0,
            "completed": 1,
            "unknown": 0,
        },
        "reasons": [],
        "observed_at": "2026-09-14T08:00:00+00:00",
    }
    child = {
        "id": "#2",
        "number": 2,
        "title": "Done child",
        "issue_state": "CLOSED",
        "state_reason": "COMPLETED",
        "project_status": "Done",
        "blocked_by": [],
        "dependency_read_complete": True,
        "readiness": "completed",
        "readiness_reasons": [],
        "observed_at": "2026-09-14T08:00:00+00:00",
    }
    snapshot = ProjectSnapshot(
        "project-1",
        "Planning",
        (
            RepositorySnapshot(
                "owner/api",
                (
                    PbiSnapshot(
                        "owner/api",
                        1,
                        "Done PBI",
                        None,
                        "Done",
                        False,
                        {
                            "pull_requests": [
                                {
                                    "number": 9,
                                    "state": "closed",
                                    "merged": True,
                                }
                            ],
                            "checks": {
                                "verdict": "unproven",
                                "pull_requests": [],
                            },
                            "issue_state": "CLOSED",
                            "state_reason": "COMPLETED",
                            "subtasks": [child],
                            "dependency_readiness": readiness,
                        },
                    ),
                ),
            ),
        ),
    )
    service = Orchestrator(OrchestratorStore(), FakeProvider(snapshot))
    client = TestClient(
        main_module.create_app(orchestrator=service, allowed_project_ids={"project-1"})
    )

    response = client.get("/projects/project-1/dashboard")

    assert response.status_code == 200
    pbi = response.json()["repositories"][0]["pbis"][0]
    assert pbi["status"] == "idle"
    assert pbi["planning_status"] == "Done"
    assert pbi["stage_label"] == "Merged"
    assert pbi["pull_requests"] == [{"number": 9, "state": "closed", "merged": True}]
    assert pbi["checks"] == {"verdict": "unproven", "pull_requests": []}
    assert pbi["issue_state"] == "CLOSED"
    assert pbi["state_reason"] == "COMPLETED"
    assert pbi["subtasks"] == [child]
    assert pbi["dependency_readiness"] == readiness
    service.store.close()


def test_live_dashboard_route_reports_repositories_and_writers() -> None:
    service = Orchestrator(OrchestratorStore(), FakeProvider(dashboard_snapshot()))
    client = TestClient(
        main_module.create_app(orchestrator=service, allowed_project_ids={"project-1"})
    )

    service.synchronize("project-1")
    api_run = service.claim("project-1", "owner/api", "worker-api")
    assert api_run is not None
    service.advance(api_run.run_id, Stage.IMPLEMENT, api_run.lease_token or "")
    service.handoff(
        api_run.run_id,
        "codex/api-one",
        None,
        "Closes #1",
        api_run.lease_token or "",
    )
    service.claim("project-1", "owner/api", "worker-api")
    service.claim("project-1", "owner/web", "worker-web")

    response = client.get("/projects/project-1/dashboard")

    assert response.status_code == 200
    payload = response.json()
    assert payload["counts"]["repositories"] == 2
    assert payload["counts"]["pbis"] == 4
    assert payload["counts"]["writers"] == 2
    assert payload["counts"]["readers"] == 2
    assert payload["counts"]["subtasks"] == 1
    assert payload["counts"]["completed_runs"] == 1
    assert [repo["name"] for repo in payload["repositories"]] == [
        "owner/api",
        "owner/web",
    ]
    assert all(repo["writer"]["status"] == "active" for repo in payload["repositories"])
    api_pbi = payload["repositories"][0]["pbis"][0]
    assert api_pbi["readers"][0]["status"] == "pass"
    assert api_pbi["escalation"]["current"] == 1
    assert api_pbi["activity"][0]["action"] == "Started review"
