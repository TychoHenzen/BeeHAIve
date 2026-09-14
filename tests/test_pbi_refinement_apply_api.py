from __future__ import annotations

from conftest import FakeProvider
from fastapi.testclient import TestClient

from beehaiive.models import ProjectSnapshot, RepositorySnapshot
from beehaiive.orchestrator import Orchestrator
from beehaiive.pbi_refinement_mutation import (
    PbiRefinementMutationError,
    PbiRefinementUpdateResult,
)
from beehaiive.storage import OrchestratorStore
from main import create_app


def test_apply_refinement_api_requires_operator_and_returns_partial_result() -> None:
    project_id = "owner:2"
    repository = "owner/repo"
    store = OrchestratorStore()
    store.sync_project(
        ProjectSnapshot(project_id, "Planning", (RepositorySnapshot(repository),))
    )
    provider = FakeProvider(
        ProjectSnapshot(project_id, "Planning", (RepositorySnapshot(repository),))
    )
    calls = []
    fail_before_write = False
    failure_message = "internal stack trace details"

    def apply(request) -> PbiRefinementUpdateResult:
        if fail_before_write:
            raise PbiRefinementMutationError(
                failure_message,
                code="preflight_failed",
                status_code=502,
            )
        calls.append(request)
        return PbiRefinementUpdateResult(
            status="partial",
            issue_number=65,
            issue_url="https://github.com/owner/repo/issues/65",
            labels=("Effort 5 - Large", "Prio 5 - Planned"),
            project_item_id="item-node",
            project_status="Backlog",
            linked_sub_issues=(),
            completed_steps=("issue_body_and_labels",),
            pending_step="project_status",
            failure_code="project_status_unconfirmed",
        )

    provider.apply_pbi_refinement = apply
    orchestrator = Orchestrator(store, provider)
    client = TestClient(
        create_app(
            orchestrator=orchestrator,
            api_key="test-key",
            allowed_project_ids={project_id},
            workflow_actor="operator",
        )
    )
    path = f"/projects/{project_id}/repositories/{repository}/pbis/65/refinement/apply"
    payload = {
        "sections": {
            "Outcome": "Outcome text.",
            "Scope": "Scope text.",
            "Implementation notes": "Implementation text.",
            "Acceptance criteria": "Acceptance text.",
            "Verification": "Verification text.",
        },
        "priority_label": "Prio 5 - Planned",
        "effort_label": "Effort 5 - Large",
        "standard_labels": [],
    }

    unauthorized = client.post(path, json=payload)
    assert unauthorized.status_code == 401
    assert unauthorized.json()["completed_steps"] == []
    assert unauthorized.json()["pending_step"] == "authorization"
    invalid = client.post(
        path,
        headers={"X-API-Key": "test-key"},
        json={**payload, "sections": {"Outcome": "signed-url-secret"}},
    )
    assert invalid.status_code == 422
    assert "signed-url-secret" not in invalid.text
    assert invalid.json()["completed_steps"] == []
    assert invalid.json()["pending_step"] == "validation"
    assert calls == []

    fail_before_write = True
    failed = client.post(path, headers={"X-API-Key": "test-key"}, json=payload)
    fail_before_write = False
    assert failed.status_code == 502
    assert failed.json()["completed_steps"] == []
    assert failed.json()["pending_step"] == "preflight"
    assert failed.json()["failure_code"] == "preflight_failed"
    assert failed.json()["message"] == "PBI refinement could not be applied"
    assert failure_message not in failed.text

    response = client.post(path, headers={"X-API-Key": "test-key"}, json=payload)
    assert response.status_code == 202
    assert response.json()["pending_step"] == "project_status"
    assert calls[0].project_id == project_id
    assert calls[0].pbi_number == 65

    writer_client = TestClient(
        create_app(
            orchestrator=orchestrator,
            api_key="test-key",
            allowed_project_ids={project_id},
            workflow_actor="writer",
        )
    )
    forbidden = writer_client.post(
        path, headers={"X-API-Key": "test-key"}, json=payload
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["completed_steps"] == []
    assert forbidden.json()["pending_step"] == "authorization"
    assert len(calls) == 1
    store.close()
