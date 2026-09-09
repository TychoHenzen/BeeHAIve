import pytest
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
from main import _configured_project_ids, create_app

client = TestClient(create_app())


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

    def validate_handoff(
        self, repository: str, branch: str, requested_base: str | None
    ) -> str:
        return self.resolve_base_branch(repository, requested_base)


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


def test_run_routes_advance_handoff_and_fail() -> None:
    service = Orchestrator(OrchestratorStore(), ApiProvider())
    project_client = TestClient(
        create_app(
            orchestrator=service,
            api_key="test-key",
            allowed_project_ids={"owner:7"},
        )
    )
    auth_headers = {"X-API-Key": "test-key", "X-Worker-ID": "worker-1"}

    project_client.post("/projects/owner:7/sync", headers=auth_headers)
    claim_response = project_client.post(
        "/projects/owner:7/repositories/owner/api/claim",
        headers=auth_headers,
    )
    run_id = claim_response.json()["run_id"]
    lease_headers = {
        "X-API-Key": "test-key",
        "X-Lease-Token": claim_response.json()["lease_token"],
    }

    state_response = project_client.get("/projects/owner:7")
    renew_response = project_client.post(f"/runs/{run_id}/lease", headers=lease_headers)
    advance_response = project_client.post(
        f"/runs/{run_id}/advance",
        json={"target": Stage.IMPLEMENT.value},
        headers=lease_headers,
    )
    handoff_response = project_client.post(
        f"/runs/{run_id}/handoff",
        json={"branch": "codex/api-1", "body": "Closes #1"},
        headers=lease_headers,
    )

    second_claim = project_client.post(
        "/projects/owner:7/repositories/owner/api/claim",
        headers=auth_headers,
    )
    failure_response = project_client.post(
        f"/runs/{second_claim.json()['run_id']}/fail",
        json={"error": "verification failed"},
        headers={
            "X-API-Key": "test-key",
            "X-Lease-Token": second_claim.json()["lease_token"],
        },
    )

    assert state_response.status_code == 200
    assert renew_response.status_code == 200
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

    def validate_handoff(
        self, repository: str, branch: str, requested_base: str | None
    ) -> str:
        raise ProviderError("cannot validate handoff")


def test_routes_map_store_and_provider_errors() -> None:
    error_client = TestClient(
        create_app(
            orchestrator=Orchestrator(OrchestratorStore(), ErrorProvider()),
            api_key="test-key",
            allowed_project_ids={"missing"},
        )
    )

    unknown_project = error_client.get("/projects/missing")
    provider_failure = error_client.post(
        "/projects/missing/sync", headers={"X-API-Key": "test-key"}
    )

    assert unknown_project.status_code == 409
    assert provider_failure.status_code == 502


def test_mutating_routes_require_authentication_and_project_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = Orchestrator(OrchestratorStore(), ApiProvider())
    project_client = TestClient(
        create_app(
            orchestrator=service,
            api_key="test-key",
            allowed_project_ids={"owner:7"},
        )
    )

    missing_key = project_client.post("/projects/owner:7/sync")
    wrong_project = project_client.post(
        "/projects/other:7/sync", headers={"X-API-Key": "test-key"}
    )
    project_client.post("/projects/owner:7/sync", headers={"X-API-Key": "test-key"})
    unauthorized_repository = project_client.post(
        "/projects/owner:7/repositories/owner/other/claim",
        headers={"X-API-Key": "test-key", "X-Worker-ID": "worker-1"},
    )
    missing_worker = project_client.post(
        "/projects/owner:7/repositories/owner/api/claim",
        headers={"X-API-Key": "test-key"},
    )
    unknown_run = project_client.post(
        "/runs/missing/lease", headers={"X-API-Key": "test-key"}
    )

    monkeypatch.delenv("BEEHAIIVE_API_KEY", raising=False)
    unconfigured_client = TestClient(
        create_app(
            orchestrator=Orchestrator(OrchestratorStore(), ApiProvider()),
            allowed_project_ids={"owner:7"},
        )
    )
    unconfigured = unconfigured_client.post(
        "/projects/owner:7/sync", headers={"X-API-Key": "test-key"}
    )

    assert missing_key.status_code == 401
    assert wrong_project.status_code == 403
    assert unauthorized_repository.status_code == 403
    assert missing_worker.status_code == 401
    assert unknown_run.status_code == 403
    assert unconfigured.status_code == 503


def test_project_scope_configuration_uses_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BEEHAIIVE_ALLOWED_PROJECTS", " owner:1, owner:2 ")
    assert _configured_project_ids(None) == {"owner:1", "owner:2"}

    monkeypatch.delenv("BEEHAIIVE_ALLOWED_PROJECTS")
    monkeypatch.setenv("GITHUB_PROJECT_OWNER", "owner")
    monkeypatch.setenv("GITHUB_PROJECT_NUMBER", "7")
    assert _configured_project_ids(None) == {"owner:7"}
