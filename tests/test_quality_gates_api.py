from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from beehaiive.quality_gates import RepositoryGateSuite
from beehaiive.workflow import (
    Constitution,
    WorkflowService,
    WorkflowStore,
)
from main import create_app
from tests.support.quality_gates.helpers import (
    make_gate,
    make_gate_repository,
    write_manifest,
)

CONSTITUTION_PATH = Path(__file__).parents[1] / "constitution.json"


def test_model_call_api_persists_gates_and_only_blocks_required_failures(
    tmp_path: Path,
) -> None:
    repository = make_gate_repository(tmp_path / "repository")
    write_manifest(
        repository,
        [
            make_gate("local", [sys.executable, "-c", "print('ready')"]),
            make_gate(
                "actionlint",
                [],
                required=False,
                external_only=True,
                category="security",
            ),
        ],
    )
    subprocess.run(("git", "add", "."), cwd=repository, check=True, capture_output=True)
    subprocess.run(
        ("git", "commit", "-m", "add gate contract"),
        cwd=repository,
        check=True,
        capture_output=True,
    )
    store = WorkflowStore(tmp_path / "workflow.db")
    service = WorkflowService(
        store,
        repository,
        Constitution.load(CONSTITUTION_PATH),
        None,
        check_runner=RepositoryGateSuite(),
    )
    lease = service.acquire_workspace("worker", "codex/gates", tmp_path / "worktree")
    client = TestClient(
        create_app(
            workflow_service=service,
            api_key="test-key",
            workflow_actor="operator",
        )
    )
    headers = {
        "X-API-Key": "test-key",
        "X-Workflow-Lease-Token": lease.lease_token or "",
    }

    passed = client.post(
        "/workflow/model-calls", json={"lease_id": lease.lease_id}, headers=headers
    )
    assert passed.status_code == 200
    passed_result = passed.json()
    assert passed_result["allowed"] is True
    assert passed_result["checks"][1]["status"] == "external_only"
    assert passed_result["checks"][1]["passed"] is False
    assert (
        service.store.latest_gate(lease.lease_id, "model_call").as_dict()
        == passed_result
    )

    write_manifest(
        Path(lease.worktree_path),
        [
            make_gate(
                "required-failure", [sys.executable, "-c", "import sys; sys.exit(4)"]
            ),
            make_gate(
                "codeql",
                [],
                required=False,
                external_only=True,
                category="security",
            ),
        ],
    )
    blocked = client.post(
        "/workflow/model-calls", json={"lease_id": lease.lease_id}, headers=headers
    )
    assert blocked.status_code == 200
    blocked_result = blocked.json()
    assert blocked_result["allowed"] is False
    assert blocked_result["checks"][0]["status"] == "failed"
    assert blocked_result["checks"][1]["status"] == "external_only"
    assert "required-failure" in blocked_result["required_action"]
    latest = service.store.latest_gate(lease.lease_id, "model_call")
    assert latest is not None and latest.as_dict() == blocked_result
    service.discard_workspace(lease.lease_id, "test complete")
    store.close()
