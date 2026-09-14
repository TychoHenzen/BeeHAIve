import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from beehaiive.agent import (
    AgentWorkerManager,
    CodexExecModelExecutor,
)
from beehaiive.models import (
    RunStatus,
)
from beehaiive.quality_gates import MANIFEST_NAME, RepositoryGateSuite
from beehaiive.workflow import (
    Constitution,
    LeaseStatus,
    WorkflowService,
    WorkflowStore,
)
from tests.support.agent.helpers import configure_test_remote as configure_test_remote
from tests.support.agent.helpers import make_git_repository as make_git_repository
from tests.support.agent.helpers import service_with_run as service_with_run


def test_dashboard_worker_writes_only_in_retained_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = make_git_repository(tmp_path / "repository")
    (repository / MANIFEST_NAME).write_text(
        json.dumps(
            {
                "version": 1,
                "gates": [
                    {
                        "name": "worker-smoke",
                        "argv": [sys.executable, "-c", "print('ready')"],
                        "timeout_seconds": 5,
                        "category": "tests",
                        "required": True,
                        "external_only": False,
                    },
                    {
                        "name": "codeql",
                        "argv": [],
                        "timeout_seconds": 1,
                        "category": "security",
                        "required": False,
                        "external_only": True,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    subprocess.run(("git", "add", MANIFEST_NAME), cwd=repository, check=True)
    subprocess.run(
        ("git", "commit", "-m", "add worker gate contract"),
        cwd=repository,
        check=True,
        capture_output=True,
    )
    remote = configure_test_remote(repository, tmp_path)
    source_head = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    script = tmp_path / "writer.py"
    script.write_text(
        "import json, os, pathlib, subprocess\n"
        "pathlib.Path('worker-output.txt').write_text('leased\\n')\n"
        "branch = subprocess.check_output(['git', 'branch', '--show-current'], "
        "text=True).strip()\n"
        "files = subprocess.check_output(['git', 'ls-files', '-z']).split(b'\\0')\n"
        "evidence = {'repository': 'owner/api', 'branch': branch, "
        "'tracked_file_count': len([item for item in files if item]), "
        "'credentials_absent': not any(name in os.environ for name in "
        "('GITHUB_TOKEN', 'BEEHAIIVE_API_KEY'))}\n"
        "payload = {'outcome': 'pass', 'evidence': evidence, 'artifact_refs': []}\n"
        "print(json.dumps({'type': 'agent_message', 'text': json.dumps(payload)}))\n",
        encoding="utf-8",
    )

    class WorkspaceScriptExecutor(CodexExecModelExecutor):
        def _workspace_command_for_execution(
            self, prompt: str, model: str, worktree: Path
        ) -> list[str]:
            return [sys.executable, str(script), "--cd", str(worktree), prompt]

    executor = WorkspaceScriptExecutor(
        repository, executable=sys.executable, repository_name="owner/api"
    )
    orchestrator, state_store, routing_store, run = service_with_run(executor)

    workflow_store = WorkflowStore(tmp_path / "workflow.db")
    workflow_service = WorkflowService(
        workflow_store,
        repository,
        Constitution.load(Path(__file__).parents[1] / "constitution.json"),
        None,
        check_runner=RepositoryGateSuite(),
    )
    monkeypatch.setenv("GITHUB_TOKEN", "fixture-secret")
    monkeypatch.setenv("BEEHAIIVE_API_KEY", "fixture-api-key")
    provider = orchestrator.provider
    original_create_handoff = provider.create_handoff
    handoff_run_states: list[RunStatus | None] = []

    def record_handoff_state(request):
        current_run = state_store.get_run(request.run_id)
        handoff_run_states.append(None if current_run is None else current_run.status)
        return original_create_handoff(request)

    monkeypatch.setattr(provider, "create_handoff", record_handoff_state)
    manager = AgentWorkerManager(orchestrator, executor, workflow_service)
    try:
        manager.start(run)
        deadline = time.monotonic() + 5
        current = state_store.get_run(run.run_id)
        while current is not None and current.status is RunStatus.ACTIVE:
            if time.monotonic() >= deadline:
                raise AssertionError("The dashboard worker did not finish")
            time.sleep(0.01)
            current = state_store.get_run(run.run_id)

        assert current is not None and current.status is RunStatus.COMPLETED
        assert current.stage.value == "pull_request"
        assert "Git delivery: pushed" in (current.last_result or "")
        assert handoff_run_states == [RunStatus.ACTIVE]
        lease = workflow_service.workspace_for_run(run.run_id)
        assert lease is not None
        assert lease.status is LeaseStatus.RETAINED
        worktree = Path(lease.worktree_path)
        assert (worktree / "worker-output.txt").read_text(
            encoding="utf-8"
        ) == "leased\n"
        assert not (repository / "worker-output.txt").exists()
        source_status = subprocess.run(
            ("git", "status", "--porcelain"),
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        )
        assert source_status.stdout == ""
        assert lease.lease_id and lease.lease_token and lease.branch
        assert executor._workspace_leases == {}
        delivered_sha = subprocess.run(
            ("git", "rev-parse", "HEAD"),
            cwd=worktree,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
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
        assert delivered_sha == remote_head
        assert f"Pushed head: {remote_head}" in (current.last_result or "")
        assert '"outcome":"pass"' in (current.last_result or "")
        handoff_record = state_store._connection.execute(
            """
            SELECT branch, pull_request_url, pull_request_number
            FROM handoffs WHERE run_id = ?
            """,
            (run.run_id,),
        ).fetchone()
        assert handoff_record is not None
        assert handoff_record["branch"] == lease.branch
        assert handoff_record["pull_request_url"].endswith("/pull/1")
        assert handoff_record["pull_request_number"] == 1
        handoff_evidence = state_store._connection.execute(
            """
            SELECT handoff_head_sha, handoff_verification_evidence
            FROM pbis WHERE project_id = ? AND repository_name = ? AND number = ?
            """,
            (run.project_id, run.repository, run.pbi_number),
        ).fetchone()
        assert handoff_evidence is not None
        assert handoff_evidence["handoff_head_sha"] == remote_head
        assert '"outcome":"pass"' in handoff_evidence["handoff_verification_evidence"]
        assert '"quality_gates":' in handoff_evidence["handoff_verification_evidence"]
        model_gate = workflow_store.latest_gate(lease.lease_id, "model_call")
        assert model_gate is not None and model_gate.allowed
        assert [check.status for check in model_gate.checks] == [
            "passed",
            "external_only",
        ]
        assert model_gate.checks[1].passed is False
        assert (
            subprocess.run(
                ("git", "status", "--porcelain"),
                cwd=worktree,
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            == ""
        )
        assert (
            subprocess.run(
                ("git", "rev-parse", "HEAD"),
                cwd=repository,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            == source_head
        )
        workflow_service.release_workspace(lease.lease_id)
        assert not worktree.exists()
    finally:
        manager.shutdown()
        workflow_store.close()
        state_store.close()
        routing_store.close()
