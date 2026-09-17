import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import beehaiive.agent_parts.values as agent_values
from beehaiive.agent import (
    CodexExecModelExecutor,
)
from beehaiive.routing import (
    AttemptOutcome,
)
from beehaiive.storage import StoreError
from tests.support.agent.repair_script_executor import (
    RepairScriptExecutor as RepairScriptExecutor,
)


def test_codex_executor_repair_honors_cancellation_and_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cancelled = RepairScriptExecutor(tmp_path, tmp_path / "unused.py")
    cancelled.prepare_run("repair-cancel-before", "owner/api")
    cancelled.cancel("repair-cancel-before")
    result = cancelled.execute_repair(
        "repair-cancel-before", tmp_path, "feature", "master"
    )
    assert result.outcome is AttemptOutcome.FAILURE
    assert "stopped" in result.failure_context

    slow_script = tmp_path / "repair-slow.py"
    slow_script.write_text("import time\ntime.sleep(30)\n")
    slow = RepairScriptExecutor(tmp_path, slow_script, timeout_seconds=0.1)
    timed_out = slow.execute_repair("repair-timeout", tmp_path, "feature", "master")
    assert timed_out.outcome is AttemptOutcome.FAILURE
    assert "timed out" in timed_out.failure_context

    class CancelOnStartExecutor(RepairScriptExecutor):
        def _start_process(
            self, command: list[str], environment: dict[str, str]
        ) -> subprocess.Popen[str]:
            process = super()._start_process(command, environment)
            self.cancel("repair-cancel-launch")
            return process

    cancelled_after_start = CancelOnStartExecutor(tmp_path, slow_script)
    stopped = cancelled_after_start.execute_repair(
        "repair-cancel-launch", tmp_path, "feature", "master"
    )
    assert stopped.outcome is AttemptOutcome.FAILURE
    assert "stopped" in stopped.failure_context

    timeout_exception = RepairScriptExecutor(tmp_path, slow_script)
    monkeypatch.setattr(
        timeout_exception,
        "_start_process",
        lambda command, environment: SimpleNamespace(returncode=0),
    )
    monkeypatch.setattr(
        timeout_exception,
        "_communicate_bounded",
        lambda process, timeout: (_ for _ in ()).throw(
            subprocess.TimeoutExpired("codex", timeout)
        ),
    )
    monkeypatch.setattr(timeout_exception, "_terminate_process", lambda process: None)
    raised_timeout = timeout_exception.execute_repair(
        "repair-timeout-exception", tmp_path, "feature", "master"
    )
    assert raised_timeout.outcome is AttemptOutcome.FAILURE
    assert "timed out" in raised_timeout.failure_context


def test_executor_validates_configuration_and_reads_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ValueError, match="does not exist"):
        CodexExecModelExecutor(tmp_path / "missing")
    with pytest.raises(ValueError, match="executable is required"):
        CodexExecModelExecutor(tmp_path, executable=" ")
    with pytest.raises(ValueError, match="timeout must be positive"):
        CodexExecModelExecutor(tmp_path, timeout_seconds=0)
    with pytest.raises(ValueError, match="task is required"):
        CodexExecModelExecutor(tmp_path, task=" ")

    monkeypatch.setenv("BEEHAIIVE_AGENT_TIMEOUT_SECONDS", "invalid")
    with pytest.raises(ValueError, match="must be a positive number"):
        CodexExecModelExecutor.from_environment()

    monkeypatch.setenv("BEEHAIIVE_AGENT_TIMEOUT_SECONDS", "3.5")
    monkeypatch.setenv("BEEHAIIVE_AGENT_REPOSITORY", str(tmp_path))
    monkeypatch.setenv("BEEHAIIVE_CODEX_EXECUTABLE", " codex-test ")
    monkeypatch.setenv("BEEHAIIVE_CODEX_MODEL", " luna-test ")
    monkeypatch.setenv("GITHUB_TOKEN", "github-secret")
    monkeypatch.setenv("BEEHAIIVE_API_KEY", "operator-secret")
    executor = CodexExecModelExecutor.from_environment()

    assert executor.repository == tmp_path.resolve()
    assert executor.executable == "codex-test"
    assert executor.timeout_seconds == 3.5
    assert executor.model == "luna-test"
    safe_environment = executor._safe_environment()
    assert "GITHUB_TOKEN" not in safe_environment
    assert "BEEHAIIVE_API_KEY" not in safe_environment
    monkeypatch.setenv("DATABASE_URL", "postgres://secret")
    monkeypatch.setenv("PIP_INDEX_URL", "https://secret.example")
    safe_environment = executor._safe_environment()
    assert "DATABASE_URL" not in safe_environment
    assert "PIP_INDEX_URL" not in safe_environment
    assert "PATH" in safe_environment

    monkeypatch.setenv("BEEHAIIVE_AGENT_TIMEOUT_SECONDS", "nan")
    with pytest.raises(ValueError, match="finite"):
        CodexExecModelExecutor.from_environment()
    monkeypatch.setenv("BEEHAIIVE_AGENT_TIMEOUT_SECONDS", "inf")
    with pytest.raises(ValueError, match="finite"):
        CodexExecModelExecutor.from_environment()


def test_executor_resolves_native_codex_and_reports_launch_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    native = tmp_path / "codex.exe"
    monkeypatch.setattr(
        agent_values.shutil,
        "which",
        lambda command: str(native) if command == "codex.exe" else None,
    )
    executor = CodexExecModelExecutor(
        tmp_path, executable="codex", repository_name="owner/api"
    )
    assert executor.executable == str(native)

    missing = tmp_path / "missing-codex.exe"
    with pytest.raises(RuntimeError) as error:
        executor._start_process([str(missing), "--cd", str(tmp_path)], {})
    message = str(error.value)
    assert "Codex launch failed" in message
    assert "missing-codex.exe" in message
    assert "filename=" in message
    assert "errno=2" in message
    assert "cwd=" in message


def test_executor_binds_repository_and_uses_selected_model(tmp_path: Path) -> None:
    executor = CodexExecModelExecutor(
        tmp_path, model="configured-model", repository_name="owner/api"
    )
    with pytest.raises(StoreError, match="identity is not configured"):
        CodexExecModelExecutor(tmp_path).validate_repository("owner/api")
    with pytest.raises(StoreError, match="does not match"):
        executor.validate_repository("owner/other")
    command = executor._command_for_execution("prompt", "selected-model", tmp_path)
    assert command[-3:] == ["--model", "selected-model", "prompt"]

    default_command = CodexExecModelExecutor(
        tmp_path, repository_name="owner/api"
    )._command_for_execution("prompt", "luna", tmp_path)
    assert "--model" not in default_command
