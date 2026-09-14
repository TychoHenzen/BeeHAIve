from fastapi.testclient import TestClient

from beehaiive.models import (
    Stage,
)
from beehaiive.orchestrator import Orchestrator
from beehaiive.storage import OrchestratorStore
from main import create_app
from tests.support.api_provider import ApiProvider


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
