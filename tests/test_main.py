from fastapi.testclient import TestClient

from beehaiive.models import (
    HandoffRequest,
    HandoffResult,
    PbiSnapshot,
    ProjectSnapshot,
    RepositorySnapshot,
    Stage,
)
from beehaiive.orchestrator import Orchestrator
from beehaiive.provider import ProviderError
from beehaiive.storage import OrchestratorStore
from main import app, create_app

client = TestClient(app)


class ApiProvider:
    def discover_project(self, project_id: str) -> ProjectSnapshot:
        return ProjectSnapshot(
            project_id=project_id,
            name="Planning",
            repositories=(
                RepositorySnapshot(
                    "owner/api",
                    (
                        PbiSnapshot("owner/api", 1, "API one"),
                        PbiSnapshot("owner/api", 2, "API two"),
                    ),
                ),
            ),
        )

    def create_handoff(self, request: HandoffRequest) -> HandoffResult:
        return HandoffResult(request.branch, "https://example.test/pull/1", 1)

    def resolve_base_branch(self, repository: str, requested: str | None) -> str:
        return requested or "master"


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
    project_client = TestClient(create_app(orchestrator=service))

    sync_response = project_client.post("/projects/owner:7/sync")
    claim_response = project_client.post(
        "/projects/owner:7/repositories/owner/api/claim"
    )

    assert sync_response.status_code == 200
    assert claim_response.status_code == 200
    assert claim_response.json()["stage"] == "refine"


def test_run_routes_advance_handoff_and_fail() -> None:
    service = Orchestrator(OrchestratorStore(), ApiProvider())
    project_client = TestClient(create_app(orchestrator=service))

    project_client.post("/projects/owner:7/sync")
    claim_response = project_client.post(
        "/projects/owner:7/repositories/owner/api/claim"
    )
    run_id = claim_response.json()["run_id"]

    state_response = project_client.get("/projects/owner:7")
    advance_response = project_client.post(
        f"/runs/{run_id}/advance",
        json={"target": Stage.IMPLEMENT.value},
    )
    handoff_response = project_client.post(
        f"/runs/{run_id}/handoff",
        json={"branch": "codex/api-1", "body": "Closes #1"},
    )

    second_claim = project_client.post("/projects/owner:7/repositories/owner/api/claim")
    failure_response = project_client.post(
        f"/runs/{second_claim.json()['run_id']}/fail",
        json={"error": "verification failed"},
    )

    assert state_response.status_code == 200
    assert advance_response.status_code == 200
    assert handoff_response.status_code == 200
    assert handoff_response.json()["stage"] == Stage.PULL_REQUEST.value
    assert failure_response.status_code == 200
    assert failure_response.json()["status"] == "failed"


class ErrorProvider:
    def discover_project(self, project_id: str) -> ProjectSnapshot:
        raise ProviderError(f"cannot discover {project_id}")

    def create_handoff(self, request: HandoffRequest) -> HandoffResult:
        raise ProviderError("cannot hand off")

    def resolve_base_branch(self, repository: str, requested: str | None) -> str:
        raise ProviderError("cannot resolve base branch")


def test_routes_map_store_and_provider_errors() -> None:
    error_client = TestClient(
        create_app(orchestrator=Orchestrator(OrchestratorStore(), ErrorProvider()))
    )

    unknown_project = error_client.get("/projects/missing")
    provider_failure = error_client.post("/projects/missing/sync")

    assert unknown_project.status_code == 409
    assert provider_failure.status_code == 502
