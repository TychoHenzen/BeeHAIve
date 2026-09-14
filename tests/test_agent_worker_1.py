from dataclasses import replace
from pathlib import Path

import pytest

from beehaiive.agent import (
    AgentWorkerManager,
    CodexExecModelExecutor,
)
from beehaiive.contracts import TaskContract
from beehaiive.models import (
    RunStatus,
    Stage,
)
from beehaiive.routing import (
    AttemptOutcome,
    ModelExecution,
)
from beehaiive.storage import StoreError
from beehaiive.workflow import (
    LeaseStatus,
)
from tests.support.agent.helpers import make_git_repository as make_git_repository
from tests.support.agent.helpers import service_with_run as service_with_run
from tests.support.agent.helpers import workflow_service_for as workflow_service_for
from tests.support.agent.immediate_executor import (
    ImmediateExecutor as ImmediateExecutor,
)


def test_writer_command_locks_sandbox_network_and_agent_policy(
    tmp_path: Path,
) -> None:
    executor = CodexExecModelExecutor(
        tmp_path, repository_name="owner/api", model="writer-model"
    )

    command = executor._workspace_command_for_execution(
        "implement", "writer-model", tmp_path
    )

    assert "workspace-write" in command
    assert "--strict-config" in command
    assert "sandbox_workspace_write.network_access=false" in command
    assert "sandbox_workspace_write.exclude_slash_tmp=true" in command
    assert "sandbox_workspace_write.exclude_tmpdir_env_var=true" in command
    assert "agents.enabled=false" in command
    assert "--add-dir" not in command
    assert "--dangerously-bypass-approvals-and-sandbox" not in command
    assert command[command.index("--cd") + 1] == str(tmp_path)


def test_prepare_run_rejects_invalid_and_credentialed_workspaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = make_git_repository(tmp_path / "repository")
    workflow_service, workflow_store = workflow_service_for(tmp_path, repository)
    lease = workflow_service.acquire_workspace(
        "prepare-test", "codex/prepare-test", tmp_path / "prepare-worktree"
    )
    executor = CodexExecModelExecutor(repository, repository_name="owner/api")

    def validate() -> None:
        return None

    try:
        with pytest.raises(StoreError, match="active workflow workspace lease"):
            executor.prepare_run(
                "prepare-test",
                "owner/api",
                replace(lease, status=LeaseStatus.RETAINED),
                validate,
            )
        with pytest.raises(StoreError, match="lease token is required"):
            executor.prepare_run(
                "prepare-test", "owner/api", replace(lease, lease_token=None), validate
            )
        with pytest.raises(StoreError, match="lease token is required"):
            executor.prepare_run("prepare-test", "owner/api", lease)
        with pytest.raises(StoreError, match="worktree is unavailable"):
            executor.prepare_run(
                "prepare-test",
                "owner/api",
                replace(lease, worktree_path=str(tmp_path / "missing-worktree")),
                validate,
            )

        monkeypatch.setattr(executor, "_repository_files", lambda: (Path(".env"),))
        with pytest.raises(StoreError, match="Credential-like tracked files"):
            executor.prepare_run("prepare-test", "owner/api", lease, validate)
        executor.prepare_run("prepare-test", "owner/api")
        try:
            with pytest.raises(StoreError, match="already prepared"):
                executor.prepare_run("prepare-test", "owner/api")
        finally:
            executor.release_run("prepare-test")
    finally:
        workflow_service.discard_workspace(lease.lease_id, "test complete")
        workflow_store.close()


def test_recovered_run_reuses_its_persisted_task_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = ImmediateExecutor(
        ModelExecution(AttemptOutcome.FAILURE, failure_context="unused")
    )
    service, store, routing_store, run = service_with_run(executor)
    lease_token = run.lease_token or ""
    contract = TaskContract.inventory(
        "owner/api", 1, "Persisted task", branch="codex/persisted", tracked_file_count=7
    )
    service.advance(run.run_id, Stage.IMPLEMENT, lease_token)
    store.ensure_task_contract(run.run_id, contract, lease_token)
    persisted_run = store.get_run(run.run_id)
    assert persisted_run is not None

    monkeypatch.setattr(
        executor,
        "build_task_contract",
        lambda _run: pytest.fail("recovery rebuilt its persisted task contract"),
    )

    assert service._task_contract_for_run(persisted_run) == contract
    store.close()
    routing_store.close()


def test_worker_manager_rejects_duplicates_and_shutdowns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = make_git_repository(tmp_path / "repository")
    executor = ImmediateExecutor(
        ModelExecution(AttemptOutcome.SUCCESS, result="unused"), repository
    )
    service, store, routing_store, run = service_with_run(executor)
    workflow_service, workflow_store = workflow_service_for(tmp_path, repository)

    class FakeThread:
        def __init__(self, target, args, name, daemon) -> None:
            self.target = target
            self.args = args

        def start(self) -> None:
            return None

        def join(self, timeout=None) -> None:
            return None

    monkeypatch.setattr(
        "beehaiive.agent_parts.worker_capacity_mixin.Thread", FakeThread
    )
    manager = AgentWorkerManager(service, executor, workflow_service)
    try:
        with pytest.raises(StoreError, match="active leased"):
            manager.start(replace(run, lease_token=None))
        manager.start(run)
        session = store.get_agent_session(run.run_id)
        assert session is not None
        assert session["session_id"] == run.run_id
        assert session["task"] == executor.task
        executor._session_event_handlers[run.run_id](
            "message", "item.completed", "assistant", "token=private"
        )
        session = store.get_agent_session(run.run_id)
        assert session is not None
        assert session["events"][0]["text"] == "token=[redacted]"
        with pytest.raises(StoreError, match="already active"):
            manager.start(run)
        manager.shutdown()
        stopped = store.get_run(run.run_id)
        assert stopped is not None
        assert stopped.status is RunStatus.FAILED
    finally:
        store.close()
        routing_store.close()
        workflow_store.close()
