from fastapi.testclient import TestClient

from beehaiive.models import (
    PbiSnapshot,
    ProjectSnapshot,
    RepositorySnapshot,
)
from beehaiive.orchestrator import Orchestrator
from beehaiive.storage import OrchestratorStore
from main import create_app
from tests.support.api_provider import ApiProvider

client = TestClient(create_app())


def test_root_returns_greeting() -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert response.json() == {"message": "Hello World"}


def test_hello_returns_name() -> None:
    response = client.get("/hello/Developer")

    assert response.status_code == 200
    assert response.json() == {"message": "Hello Developer"}


def test_project_routes_sync_and_claim_repository_work() -> None:
    service = Orchestrator(OrchestratorStore(), ApiProvider())
    project_client = TestClient(
        create_app(
            orchestrator=service,
            api_key="test-key",
            allowed_project_ids={"owner:7"},
        )
    )
    auth_headers = {"X-API-Key": "test-key", "X-Worker-ID": "worker-1"}

    sync_response = project_client.post("/projects/owner:7/sync", headers=auth_headers)
    claim_response = project_client.post(
        "/projects/owner:7/repositories/owner/api/claim",
        headers=auth_headers,
    )

    assert sync_response.status_code == 200
    assert claim_response.status_code == 200
    assert claim_response.json()["stage"] == "refine"


def test_project_api_exposes_dependency_readiness_metadata() -> None:
    readiness = {
        "status": "unknown",
        "counts": {
            "ready": 0,
            "incomplete": 0,
            "blocked": 0,
            "rejected": 0,
            "completed": 0,
            "unknown": 1,
        },
        "reasons": ["child_evidence_unknown"],
        "observed_at": "2026-09-14T08:00:00+00:00",
    }
    child = {
        "id": "#2",
        "number": 2,
        "title": "Unknown child",
        "issue_state": "OPEN",
        "state_reason": None,
        "project_status": None,
        "blocked_by": [],
        "dependency_read_complete": False,
        "dependency_read_error": "permission_denied",
        "readiness": "unknown",
        "readiness_reasons": ["permission_denied"],
        "observed_at": readiness["observed_at"],
    }
    store = OrchestratorStore()
    store.sync_project(
        ProjectSnapshot(
            "owner:7",
            "Planning",
            (
                RepositorySnapshot(
                    "owner/api",
                    (
                        PbiSnapshot(
                            "owner/api",
                            1,
                            "Parent PBI",
                            metadata={
                                "issue_state": "OPEN",
                                "state_reason": None,
                                "subtasks": [child],
                                "dependency_readiness": readiness,
                            },
                        ),
                    ),
                ),
            ),
        )
    )
    project_client = TestClient(
        create_app(
            orchestrator=Orchestrator(store, ApiProvider()),
            api_key="test-key",
            allowed_project_ids={"owner:7"},
        )
    )

    response = project_client.get(
        "/projects/owner:7", headers={"X-API-Key": "test-key"}
    )

    assert response.status_code == 200
    pbi = response.json()["repositories"][0]["pbis"][0]
    assert pbi["metadata"]["subtasks"] == [child]
    assert pbi["metadata"]["dependency_readiness"] == readiness
    store.close()
