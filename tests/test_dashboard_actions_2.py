import pytest
from fastapi.testclient import TestClient

import main as main_module
from beehaiive.orchestrator import Orchestrator
from beehaiive.storage import (
    OrchestratorStore,
    StoreError,
)
from tests.conftest import FakeProvider
from tests.support.dashboard.helpers import dashboard_snapshot


def test_dashboard_actions_preserve_state_and_report_results() -> None:
    service = Orchestrator(OrchestratorStore(), FakeProvider(dashboard_snapshot()))

    class RecordingWorker:
        def claim(self, project_id, repository, owner_id, task):
            del task
            return service.claim(project_id, repository, owner_id)

        def start(self, run) -> None:
            del run

        def cancel(self, run_id: str) -> None:
            del run_id

    client = TestClient(
        main_module.create_app(
            orchestrator=service,
            api_key="test-key",
            allowed_project_ids={"project-1"},
            agent_worker=RecordingWorker(),
        )
    )
    auth = {"X-API-Key": "test-key"}
    service.synchronize("project-1")

    start = client.post(
        "/projects/project-1/actions",
        headers=auth,
        json={"action": "start", "approved": True, "repository": "owner/api"},
    )
    assert start.status_code == 200
    assert start.json()["action"]["status"] == "succeeded"
    run_id = start.json()["state"]["repositories"][0]["pbis"][0]["run_id"]

    busy = client.post(
        "/projects/project-1/actions",
        headers=auth,
        json={
            "action": "start",
            "approved": True,
            "repository": "owner/api",
            "worker_id": "second-worker",
        },
    )
    assert busy.json()["action"]["status"] == "failed"

    failed = client.post(
        "/projects/project-1/actions",
        headers=auth,
        json={
            "action": "clarify",
            "approved": True,
            "repository": "owner/api",
            "pbi_number": 1,
            "run_id": run_id,
        },
    )
    assert failed.status_code == 200
    assert failed.json()["action"]["status"] == "failed"
    assert failed.json()["state"]["counts"]["active_runs"] == 1

    nonexistent_pbi = client.post(
        "/projects/project-1/actions",
        headers=auth,
        json={
            "action": "approve",
            "approved": True,
            "repository": "owner/api",
            "pbi_number": 999,
            "run_id": run_id,
        },
    )
    assert nonexistent_pbi.status_code == 403

    approved = client.post(
        "/projects/project-1/actions",
        headers=auth,
        json={
            "action": "approve",
            "approved": True,
            "repository": "owner/api",
            "pbi_number": 1,
            "run_id": run_id,
        },
    )
    assert approved.json()["action"]["status"] == "succeeded"

    clarification = client.post(
        "/projects/project-1/actions",
        headers=auth,
        json={
            "action": "clarify",
            "approved": True,
            "repository": "owner/api",
            "pbi_number": 1,
            "run_id": run_id,
            "clarification": "Use the current repository default branch.",
        },
    )
    assert clarification.json()["action"]["status"] == "succeeded"

    stopped = client.post(
        "/projects/project-1/actions",
        headers=auth,
        json={
            "action": "stop",
            "approved": True,
            "repository": "owner/api",
            "run_id": run_id,
            "reason": "operator stop",
        },
    )
    assert stopped.status_code == 200
    assert stopped.json()["action"]["status"] == "succeeded"
    assert stopped.json()["state"]["counts"]["failed_runs"] == 1

    inactive_approval = client.post(
        "/projects/project-1/actions",
        headers=auth,
        json={
            "action": "approve",
            "approved": True,
            "repository": "owner/api",
            "pbi_number": 1,
            "run_id": run_id,
        },
    )
    assert inactive_approval.status_code == 403

    assert client.get("/projects/project-1/actions").status_code == 200
    assert service.stop(run_id).status.value == "failed"

    no_approval = client.post(
        "/projects/project-1/actions",
        headers=auth,
        json={
            "action": "approve",
            "repository": "owner/api",
            "pbi_number": 1,
            "run_id": run_id,
        },
    )
    assert no_approval.status_code == 400

    missing_target = client.post(
        "/projects/project-1/actions",
        headers=auth,
        json={
            "action": "approve",
            "approved": True,
            "repository": "owner/api",
            "pbi_number": 1,
        },
    )
    assert missing_target.status_code == 422

    wrong_run = client.post(
        "/projects/project-1/actions",
        headers=auth,
        json={
            "action": "approve",
            "approved": True,
            "repository": "owner/api",
            "pbi_number": 1,
            "run_id": "wrong-run",
        },
    )
    assert wrong_run.status_code == 403

    unauthorized_repository = client.post(
        "/projects/project-1/actions",
        headers=auth,
        json={
            "action": "start",
            "approved": True,
            "repository": "owner/missing",
        },
    )
    assert unauthorized_repository.status_code == 403

    missing_run = client.post(
        "/projects/project-1/actions",
        headers=auth,
        json={"action": "stop", "approved": True},
    )
    assert missing_run.status_code == 422

    unknown_run = client.post(
        "/projects/project-1/actions",
        headers=auth,
        json={"action": "stop", "approved": True, "run_id": "missing"},
    )
    assert unknown_run.status_code == 403

    mismatched_repository = client.post(
        "/projects/project-1/actions",
        headers=auth,
        json={
            "action": "stop",
            "approved": True,
            "repository": "owner/web",
            "run_id": run_id,
        },
    )
    assert mismatched_repository.status_code == 403

    synced = client.post(
        "/projects/project-1/actions?archived=true",
        headers=auth,
        json={"action": "start", "approved": True},
    )
    assert synced.json()["action"]["status"] == "succeeded"

    with pytest.raises(StoreError, match="Unknown run"):
        main_module._execute_dashboard_action(
            service,
            "project-1",
            main_module.DashboardStopRequest(
                action="stop", approved=True, run_id="missing"
            ),
        )
