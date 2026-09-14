from __future__ import annotations

from fastapi.testclient import TestClient

from beehaiive.models import ProjectSnapshot, RepositorySnapshot
from beehaiive.orchestrator import Orchestrator
from beehaiive.storage import OrchestratorStore
from main import create_app
from tests.conftest import FakeProvider

PROJECT_ID = "project-1"

REPOSITORY = "owner/api"


def _seed_store(store: OrchestratorStore) -> None:
    store.sync_project(
        ProjectSnapshot(PROJECT_ID, "Planning", (RepositorySnapshot(REPOSITORY),))
    )


def test_refinement_api_requires_project_repository_key_and_operator() -> None:
    store = OrchestratorStore()
    _seed_store(store)
    provider = FakeProvider(
        ProjectSnapshot(PROJECT_ID, "Planning", (RepositorySnapshot(REPOSITORY),))
    )
    service = Orchestrator(store, provider)
    client = TestClient(
        create_app(
            orchestrator=service,
            api_key="test-key",
            allowed_project_ids={PROJECT_ID},
            workflow_actor="operator",
        )
    )
    path = f"/projects/{PROJECT_ID}/repositories/{REPOSITORY}/pbis/64/refinement"
    payload = {"questions": [{"text": "Which behavior is required?"}]}

    assert client.post(path, json=payload).status_code == 401
    signed_url = "https://example.test/file?X-Amz-Signature=private-signature"
    invalid = client.post(
        path,
        headers={"X-API-Key": "test-key"},
        json={"questions": [{"text": "Question?", "evidence_refs": [signed_url] * 11}]},
    )
    assert invalid.status_code == 422
    assert "private-signature" not in invalid.text
    assert signed_url not in invalid.text

    first = client.post(path, headers={"X-API-Key": "test-key"}, json=payload)
    assert first.status_code == 200
    created = client.post(path, headers={"X-API-Key": "test-key"}, json=payload).json()
    assert created["attempt_id"] == first.json()["attempt_id"]
    attempt_id = created["attempt_id"]
    question_id = created["questions"][0]["question_id"]
    answered = client.post(
        f"{path}/questions/{question_id}/answer",
        headers={"X-API-Key": "test-key"},
        json={"answer": "The answer mentions test-key.", "expected_revision": 0},
    )
    assert answered.status_code == 200
    assert answered.json()["attempt_id"] == attempt_id
    assert answered.json()["status"] == "evaluating"
    assert "test-key" not in answered.text

    read_back = client.get(path, headers={"X-API-Key": "test-key"})
    assert read_back.status_code == 200
    assert read_back.json()["attempt_id"] == attempt_id
    assert read_back.json()["questions"][0]["answer"] == (
        "The answer mentions [redacted]."
    )
    stored = store._connection.execute(
        "SELECT questions_json FROM pbi_refinement_attempts WHERE attempt_id = ?",
        (attempt_id,),
    ).fetchone()
    assert stored is not None
    assert "test-key" not in stored["questions_json"]

    wrong_project = client.get(
        "/projects/other/repositories/owner/api/pbis/64/refinement",
        headers={"X-API-Key": "test-key"},
    )
    assert wrong_project.status_code == 403
    wrong_repository = client.get(
        "/projects/project-1/repositories/owner/other/pbis/64/refinement",
        headers={"X-API-Key": "test-key"},
    )
    assert wrong_repository.status_code == 403
    writer_client = TestClient(
        create_app(
            orchestrator=service,
            api_key="test-key",
            allowed_project_ids={PROJECT_ID},
            workflow_actor="writer",
        )
    )
    wrong_role = writer_client.post(
        path,
        headers={"X-API-Key": "test-key"},
        json={"questions": [{"text": "Another attempt?"}]},
    )
    assert wrong_role.status_code == 403
    assert writer_client.get(path, headers={"X-API-Key": "test-key"}).status_code == 403
    assert provider.discoveries == 0
    assert provider.handoffs == []
    store.close()
