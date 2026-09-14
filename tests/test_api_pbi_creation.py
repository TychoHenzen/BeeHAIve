from dataclasses import replace

from fastapi.testclient import TestClient

from beehaiive.orchestrator import Orchestrator
from beehaiive.storage import OrchestratorStore
from main import create_app
from tests.support.api_provider import ApiProvider


def test_create_project_pbi_is_authenticated_scoped_and_idempotent() -> None:
    store = OrchestratorStore()
    provider = ApiProvider()
    project_client = TestClient(
        create_app(
            orchestrator=Orchestrator(store, provider),
            api_key="test-key",
            allowed_project_ids={"owner:7"},
        )
    )
    path = "/projects/owner:7/pbis"
    payload = {
        "repository": "owner/api",
        "title": "API-created PBI",
        "body": "Preserve this body.",
        "labels": ["enhancement"],
    }

    unauthenticated = project_client.post(
        path,
        headers={"Idempotency-Key": "request-1"},
        json=payload,
    )
    missing_key = project_client.post(
        path,
        headers={"X-API-Key": "test-key"},
        json=payload,
    )
    unauthorized_project = project_client.post(
        "/projects/owner:8/pbis",
        headers={"X-API-Key": "test-key", "Idempotency-Key": "request-2"},
        json=payload,
    )
    headers = {"X-API-Key": "test-key", "Idempotency-Key": "request-3"}
    created = project_client.post(path, headers=headers, json=payload)
    replay = project_client.post(path, headers=headers, json=payload)
    changed_request = project_client.post(
        path,
        headers=headers,
        json={**payload, "body": "Changed body."},
    )

    assert unauthenticated.status_code == 401
    assert missing_key.status_code == 422
    assert unauthorized_project.status_code == 403
    assert created.status_code == 201
    assert created.json()["issue"]["url"] == "https://example.test/issues/3"
    assert created.json()["project"]["status"] == "Backlog"
    assert replay.json() == created.json()
    assert changed_request.status_code == 409
    assert provider.pbi_create_calls == 1
    store.close()


def test_create_project_pbi_returns_redacted_incomplete_result() -> None:
    class IncompleteProvider(ApiProvider):
        def create_pbi(self, request, target, progress, checkpoint):
            del request, target
            checkpoint(
                replace(
                    progress,
                    issue_create_started=False,
                    issue_id="issue-node",
                    issue_number=4,
                    issue_url="https://example.test/issues/4",
                    completed_steps=("issue_created",),
                    current_step="add_to_project",
                )
            )
            raise RuntimeError("private-provider-detail")

    store = OrchestratorStore()
    project_client = TestClient(
        create_app(
            orchestrator=Orchestrator(store, IncompleteProvider()),
            api_key="test-key",
            allowed_project_ids={"owner:7"},
        )
    )
    response = project_client.post(
        "/projects/owner:7/pbis",
        headers={"X-API-Key": "test-key", "Idempotency-Key": "request-incomplete"},
        json={
            "repository": "owner/api",
            "title": "API-created PBI",
            "body": "Preserve this body.",
        },
    )

    assert response.status_code == 202
    assert response.json()["status"] == "incomplete"
    assert response.json()["issue"]["number"] == 4
    assert response.json()["completed_steps"] == ["issue_created"]
    assert response.json()["failure"]["type"] == "RuntimeError"
    assert "private-provider-detail" not in response.text
    store.close()
