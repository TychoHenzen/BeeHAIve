import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import beehaiive.agent as agent_module
from beehaiive.agent import (
    CodexExecModelExecutor,
)
from beehaiive.routing import (
    AttemptOutcome,
    ModelRouter,
    ModelTier,
    RoutingStore,
)
from beehaiive.workflow import (
    LeaseStatus,
    WorkflowError,
    WorkspaceLease,
)
from tests.support.agent.helpers import make_git_repository as make_git_repository
from tests.support.agent.script_executor import ScriptExecutor as ScriptExecutor


def test_executor_rejects_missing_and_lost_workspace_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = make_git_repository(tmp_path / "repository")
    executor = CodexExecModelExecutor(repository, repository_name="owner/api")
    routing_store = RoutingStore()
    router = ModelRouter(routing_store)
    decision = router.begin("leased-validation").decision
    spec = router.config.spec_for(ModelTier.LUNA)
    lease = WorkspaceLease(
        "lease-1",
        "dashboard-run:leased-validation",
        "codex/leased-validation",
        str(repository),
        LeaseStatus.ACTIVE,
        "created",
        "updated",
        "lease-token",
    )
    executor._workspace_leases[decision.problem_id] = lease

    try:
        missing_validator = executor.execute(spec, decision)
        assert missing_validator.outcome is AttemptOutcome.FAILURE
        assert "validator is missing" in (missing_validator.failure_context or "")

        executor._workspace_validators[decision.problem_id] = lambda: None
        executor._workspace_leases[decision.problem_id] = replace(
            lease, worktree_path=str(tmp_path / "missing-worktree")
        )
        missing_worktree = executor.execute(spec, decision)
        assert missing_worktree.outcome is AttemptOutcome.FAILURE
        assert "worktree does not exist" in (missing_worktree.failure_context or "")

        executor._workspace_leases[decision.problem_id] = lease
        validations = 0

        def lose_lease_after_launch() -> None:
            nonlocal validations
            validations += 1
            if validations == 2:
                raise WorkflowError("lease changed")

        executor._workspace_validators[decision.problem_id] = lose_lease_after_launch
        monkeypatch.setattr(
            executor,
            "_workspace_command_for_execution",
            lambda _prompt, _model, worktree: [
                sys.executable,
                "-c",
                "print('{}')",
                "--cd",
                str(worktree),
            ],
        )
        lost_lease = executor.execute(spec, decision)
        assert lost_lease.outcome is AttemptOutcome.FAILURE
        assert "lease changed" in (lost_lease.failure_context or "")
    finally:
        executor.release_run(decision.problem_id)
        routing_store.close()


def test_executor_prompt_contains_verified_repository_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor = CodexExecModelExecutor(tmp_path, repository_name="owner/api")
    executor.set_session_task("prompt-metadata", "persisted executable task")
    monkeypatch.setattr(executor, "_discover_repository_branch", lambda: "feature/demo")
    monkeypatch.setattr(
        executor,
        "_repository_files",
        lambda: (Path("README.md"), Path("main.py")),
    )

    router = ModelRouter(RoutingStore())
    spec = router.config.spec_for(ModelTier.LUNA)
    prompt = executor._prompt(spec, router.begin("prompt-metadata").decision)
    default_prompt = executor._prompt(spec, router.begin("default-task").decision)

    assert "Verified current branch: feature/demo" in prompt
    assert "Verified tracked file count: 2" in prompt
    assert "Task: persisted executable task" in prompt
    assert ".git" in default_prompt


def test_executor_safe_checkout_excludes_local_secret_files(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("safe\n", encoding="utf-8")
    (tmp_path / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    (tmp_path / ".env.production").write_text("TOKEN=secret\n", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("secret\n", encoding="utf-8")
    (tmp_path / ".env.example").write_text("TOKEN=\n", encoding="utf-8")
    (tmp_path / "id_rsa").write_text("private key\n", encoding="utf-8")
    (tmp_path / "id_ed25519").write_text("private key\n", encoding="utf-8")
    (tmp_path / "service-account.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "application.pem").write_text("private key\n", encoding="utf-8")
    (tmp_path / "application.p12").write_text("private key\n", encoding="utf-8")
    executor = CodexExecModelExecutor(tmp_path, repository_name="owner/api")

    with executor._safe_checkout() as checkout:
        assert (checkout / "README.md").is_file()
        assert (checkout / ".env.example").is_file()
        assert not (checkout / ".env").exists()
        assert not (checkout / ".env.production").exists()
        assert not (checkout / "secret.txt").exists()
        assert not (checkout / "id_rsa").exists()
        assert not (checkout / "id_ed25519").exists()
        assert not (checkout / "service-account.json").exists()
        assert not (checkout / "application.pem").exists()
        assert not (checkout / "application.p12").exists()


def test_executor_repository_discovery_handles_git_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_run = agent_module.subprocess.run

    def git_run(command, **kwargs):
        if command[-3:] == ["config", "--get", "remote.origin.url"]:
            return SimpleNamespace(stdout="git@github.com:owner/api.git", returncode=0)
        return original_run(command, **kwargs)

    monkeypatch.setattr(agent_module.subprocess, "run", git_run)
    executor = CodexExecModelExecutor(tmp_path)
    assert executor.repository_name == "owner/api"

    def broken_run(*args, **kwargs):
        del args, kwargs
        raise OSError("git unavailable")

    monkeypatch.setattr(agent_module.subprocess, "run", broken_run)
    assert CodexExecModelExecutor(tmp_path).repository_name is None
    assert (
        CodexExecModelExecutor(
            tmp_path, repository_name="owner/api"
        )._discover_repository_branch()
        == "unknown"
    )


def test_executor_handles_communicate_timeout_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor = ScriptExecutor(tmp_path, tmp_path / "unused.py")
    router = ModelRouter(RoutingStore())

    monkeypatch.setattr(
        executor, "_start_process", lambda command, environment: object()
    )

    def raise_timeout(process, timeout):
        del process, timeout
        raise subprocess.TimeoutExpired("codex", 1)

    monkeypatch.setattr(executor, "_communicate_bounded", raise_timeout)
    monkeypatch.setattr(
        CodexExecModelExecutor,
        "_terminate_process",
        staticmethod(lambda process: None),
    )

    result = executor.execute(
        router.config.spec_for(ModelTier.LUNA),
        router.begin("communicate-timeout").decision,
    )

    assert result.outcome is AttemptOutcome.FAILURE
    assert "timed out" in result.failure_context
