from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from main import create_app
from tests.support.workflow.helpers import commit_repository_change, make_service

CONSTITUTION_PATH = Path(__file__).parents[1] / "constitution.json"


def test_workflow_api_exposes_gates_and_operator_controls(tmp_path: Path) -> None:
    service, store, _ = make_service(tmp_path)
    client = TestClient(
        create_app(
            workflow_service=service,
            api_key="test-key",
            workflow_actor="operator",
        )
    )
    auth = {"X-API-Key": "test-key"}
    no_auth = client.post("/workflow/model-calls", json={"lease_id": "missing"})
    assert no_auth.status_code == 401

    worktree = tmp_path / "api-worktree"
    acquired = client.post(
        "/workflow/workspaces",
        json={
            "agent_id": "writer-api",
            "branch": "codex/api-workflow",
            "worktree": str(worktree),
        },
        headers=auth,
    )
    assert acquired.status_code == 200
    lease_id = acquired.json()["lease_id"]
    lease_auth = {
        **auth,
        "X-Workflow-Lease-Token": acquired.json()["lease_token"],
    }
    invalid_token = client.post(
        "/workflow/model-calls",
        json={"lease_id": lease_id},
        headers=auth,
    )
    assert invalid_token.status_code == 409
    renewed = client.post(f"/workflow/workspaces/{lease_id}/renew", headers=lease_auth)
    assert renewed.status_code == 200
    invalid_role = client.post(
        "/workflow/handoffs",
        json={
            "lease_id": lease_id,
            "source_role": "not-a-role",
            "target_role": "operator",
        },
        headers=lease_auth,
    )
    assert invalid_role.status_code == 422
    assert client.post(
        "/workflow/model-calls", json={"lease_id": lease_id}, headers=lease_auth
    ).json()["allowed"]
    commit_sha = commit_repository_change(worktree, "api.txt", "api\n")

    waiting = client.post(
        "/workflow/handoffs",
        json={
            "lease_id": lease_id,
            "source_role": "writer",
            "target_role": "operator",
            "commit_sha": commit_sha,
            "source_state": "ready",
            "approval_required": True,
        },
        headers=lease_auth,
    )
    assert waiting.status_code == 200
    handoff_id = waiting.json()["handoff_id"]
    pending_gate = client.post(
        "/workflow/model-calls",
        json={"lease_id": lease_id},
        headers=lease_auth,
    )
    assert pending_gate.status_code == 200
    assert pending_gate.json()["allowed"] is False
    assert "approval" in pending_gate.json()["required_action"].lower()
    duplicate_waiting = client.post(
        "/workflow/handoffs",
        json={
            "lease_id": lease_id,
            "source_role": "writer",
            "target_role": "operator",
            "commit_sha": commit_sha,
            "source_state": "duplicate",
        },
        headers=lease_auth,
    )
    assert duplicate_waiting.status_code == 409
    unresolved_release = client.post(
        f"/workflow/workspaces/{lease_id}/release", headers=lease_auth
    )
    assert unresolved_release.status_code == 409
    assert worktree.exists()
    assert (
        client.get(f"/workflow/handoffs/{handoff_id}", headers=auth).status_code == 200
    )
    clarified = client.post(
        f"/workflow/handoffs/{handoff_id}/clarify",
        json={"question": "Confirm the scope"},
        headers=lease_auth,
    )
    answered = client.post(
        f"/workflow/handoffs/{handoff_id}/clarify/answer",
        json={"answer": "The API only"},
        headers=lease_auth,
    )
    assert clarified.status_code == 200
    assert answered.status_code == 200
    blocked_gate = client.post(
        "/workflow/model-calls",
        json={"lease_id": lease_id},
        headers=lease_auth,
    )
    assert blocked_gate.status_code == 200
    assert blocked_gate.json()["allowed"] is False
    second_waiting = client.post(
        "/workflow/handoffs",
        json={
            "lease_id": lease_id,
            "source_role": "writer",
            "target_role": "operator",
            "commit_sha": commit_sha,
            "source_state": "ready again",
            "approval_required": True,
        },
        headers=lease_auth,
    )
    assert second_waiting.status_code == 200
    second_handoff_id = second_waiting.json()["handoff_id"]
    approved = client.post(
        f"/workflow/handoffs/{second_handoff_id}/approve",
        json={"actor": "writer", "note": "go"},
        headers=lease_auth,
    )
    assert approved.status_code == 200
    assert approved.json()["approval_actor"] == "operator"
    unknown = client.get("/workflow/handoffs/missing", headers=auth)
    assert unknown.status_code == 409

    release_worktree = tmp_path / "api-release"
    release = client.post(
        "/workflow/workspaces",
        json={
            "agent_id": "writer-release",
            "branch": "codex/api-release",
            "worktree": str(release_worktree),
        },
        headers=auth,
    )
    release_id = release.json()["lease_id"]
    release_auth = {
        **auth,
        "X-Workflow-Lease-Token": release.json()["lease_token"],
    }
    released = client.post(
        f"/workflow/workspaces/{release_id}/release", headers=release_auth
    )
    assert released.status_code == 200

    stop_worktree = tmp_path / "api-stop"
    stop = client.post(
        "/workflow/workspaces",
        json={
            "agent_id": "writer-stop",
            "branch": "codex/api-stop",
            "worktree": str(stop_worktree),
        },
        headers=auth,
    )
    stop_id = stop.json()["lease_id"]
    stop_auth = {
        **auth,
        "X-Workflow-Lease-Token": stop.json()["lease_token"],
    }
    stopped = client.post(
        f"/workflow/workspaces/{stop_id}/stop",
        json={"reason": "operator stop"},
        headers=stop_auth,
    )
    assert stopped.status_code == 200
    cleaned = client.post(f"/workflow/workspaces/{stop_id}/release", headers=stop_auth)
    assert cleaned.status_code == 200
    assert not stop_worktree.exists()
    missing_service = TestClient(create_app(api_key="test-key"))
    unavailable = missing_service.post(
        "/workflow/model-calls", json={"lease_id": "missing"}, headers=auth
    )
    assert unavailable.status_code == 503
    store.close()
