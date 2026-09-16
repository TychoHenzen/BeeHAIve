import subprocess
import time
from pathlib import Path

from fastapi.testclient import TestClient

import main as main_module
from beehaiive.agent import AgentWorkerManager
from beehaiive.orchestrator import Orchestrator
from beehaiive.routing import ModelExecution, ModelRouter, RoutingStore
from beehaiive.storage import (
    OrchestratorStore,
)
from beehaiive.workflow import (
    CheckResult,
    Constitution,
    LeaseStatus,
    WorkflowService,
    WorkflowStore,
)
from tests.conftest import FakeProvider
from tests.support.dashboard.helpers import (
    configure_dashboard_remote,
    dashboard_snapshot,
    make_dashboard_git_repository,
)
from tests.support.dashboard.immediate_demo_executor import (
    ImmediateDemoExecutor as ImmediateDemoExecutor,
)


def test_dashboard_retries_a_blocked_push_without_recommitting(tmp_path: Path) -> None:
    repository = make_dashboard_git_repository(tmp_path / "repository")
    remote = configure_dashboard_remote(repository, tmp_path)

    class WritingDemoExecutor(ImmediateDemoExecutor):
        def execute(self, spec, decision) -> ModelExecution:
            workspace = self._execution_repository_for(decision.problem_id)
            (workspace / "change.txt").write_text("save me\n", encoding="utf-8")
            return super().execute(spec, decision)

    executor = WritingDemoExecutor(repository)
    service = Orchestrator(
        OrchestratorStore(),
        FakeProvider(dashboard_snapshot()),
        ModelRouter(RoutingStore()),
        executor,
    )
    workflow_store = WorkflowStore(tmp_path / "workflow.db")

    class PassingCheck:
        name = "fixture"

        def run(self, workspace: Path) -> CheckResult:
            assert workspace.exists()
            return CheckResult(self.name, True, "passed")

    workflow_service = WorkflowService(
        workflow_store,
        repository,
        Constitution.load(Path(__file__).parents[1] / "constitution.json"),
        [PassingCheck()],
    )
    worker = AgentWorkerManager(service, executor, workflow_service)
    offline = remote.with_name("api.offline")
    remote.rename(offline)
    client = TestClient(
        main_module.create_app(
            orchestrator=service,
            api_key="test-key",
            allowed_project_ids={"project-1"},
            agent_worker=worker,
            workflow_service=workflow_service,
        )
    )
    try:
        service.synchronize("project-1")
        started = client.post(
            "/projects/project-1/actions",
            headers={"X-API-Key": "test-key"},
            json={"action": "start", "approved": True, "repository": "owner/api"},
        )
        assert started.status_code == 200
        run_id = started.json()["result"]["run"]["run_id"]
        deadline = time.monotonic() + 3
        run = service.store.get_run(run_id)
        while run is not None and run.status.value == "active":
            if time.monotonic() >= deadline:
                raise AssertionError("The worker did not preserve the blocked commit")
            time.sleep(0.02)
            run = service.store.get_run(run_id)
        assert run is not None and run.status.value == "completed"
        lease = workflow_service.workspace_for_run(run_id)
        assert lease is not None and lease.status is LeaseStatus.RETAINED
        saved_sha = workflow_service.worktrees.head(lease.worktree_path)
        assert "Git delivery: blocked" in (run.last_result or "")

        blocked_state = client.get("/projects/project-1/dashboard").json()
        delivery = blocked_state["repositories"][0]["pbis"][0]["delivery"]
        assert delivery["status"] == "push_failed"
        assert delivery["commit_sha"] == saved_sha
        assert delivery["retry_available"] is True

        blocked_action = client.post(
            "/projects/project-1/actions",
            headers={"X-API-Key": "test-key"},
            json={
                "action": "commit_push",
                "approved": True,
                "repository": "owner/api",
                "pbi_number": 1,
                "run_id": run_id,
            },
        )
        assert blocked_action.status_code == 200
        assert blocked_action.json()["action"]["status"] == "failed"
        assert blocked_action.json()["result"]["delivery"]["status"] == "blocked"

        offline.rename(remote)
        retried = client.post(
            "/projects/project-1/actions",
            headers={"X-API-Key": "test-key"},
            json={
                "action": "commit_push",
                "approved": True,
                "repository": "owner/api",
                "pbi_number": 1,
                "run_id": run_id,
            },
        )

        assert retried.status_code == 200
        assert retried.json()["action"]["status"] == "succeeded"
        assert retried.json()["result"]["delivery"]["status"] == "pushed"
        assert retried.json()["result"]["delivery"]["commit_sha"] == saved_sha
        delivered_state = retried.json()["state"]["repositories"][0]["pbis"][0][
            "delivery"
        ]
        assert delivered_state["status"] == "pushed"
        assert delivered_state["retry_available"] is False
        remote_head = subprocess.run(
            (
                "git",
                "ls-remote",
                "--exit-code",
                str(remote),
                f"refs/heads/{lease.branch}",
            ),
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.split()[0]
        assert remote_head == saved_sha
        assert workflow_service.worktrees.clean(lease.worktree_path)
    finally:
        worker.shutdown()
        if offline.exists():
            offline.rename(remote)
        workflow_store.close()
        service.store.close()
        service.model_router.store.close()
