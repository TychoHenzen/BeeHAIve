import json
import subprocess
import time
from pathlib import Path
from threading import Thread
from types import SimpleNamespace

import pytest

import beehaiive.agent as agent_module
from beehaiive.agent import (
    CodexExecModelExecutor,
)
from beehaiive.routing import (
    AttemptOutcome,
    ModelExecution,
    ModelRouter,
    ModelTier,
    RoutingStore,
)
from tests.support.agent.script_executor import ScriptExecutor as ScriptExecutor


def test_executor_safe_checkout_skips_unsafe_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path.parent / "outside-agent-file.txt"
    outside.write_text("outside\n", encoding="utf-8")
    (tmp_path / "directory").mkdir()
    executor = CodexExecModelExecutor(tmp_path, repository_name="owner/api")
    monkeypatch.setattr(
        executor,
        "_repository_files",
        lambda: (Path("../outside-agent-file.txt"), Path("directory")),
    )

    with executor._safe_checkout() as checkout:
        assert not (checkout / "outside-agent-file.txt").exists()
        assert not (checkout / "directory").exists()


def test_executor_repository_file_discovery_uses_git_and_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor = CodexExecModelExecutor(tmp_path, repository_name="owner/api")

    def git_timeout(*args, **kwargs):
        del args, kwargs
        raise subprocess.TimeoutExpired("git", 5)

    monkeypatch.setattr(agent_module.subprocess, "run", git_timeout)
    (tmp_path / "fallback.py").write_text("pass\n", encoding="utf-8")
    assert executor._repository_files() == (Path("fallback.py"),)

    monkeypatch.setattr(
        agent_module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout=b"one.py\0nested/two.py\0"
        ),
    )
    assert executor._repository_files() == (Path("one.py"), Path("nested/two.py"))


def test_executor_honors_cancellation_before_and_during_launch(
    tmp_path: Path,
) -> None:
    router = ModelRouter(RoutingStore())
    executor = ScriptExecutor(tmp_path, tmp_path / "unused.py")
    executor.prepare_run("cancel-before-launch", "owner/api")
    executor.cancel("cancel-before-launch")
    result = executor.execute(
        router.config.spec_for(ModelTier.LUNA),
        router.begin("cancel-before-launch").decision,
    )
    assert result.outcome is AttemptOutcome.FAILURE
    assert "stopped" in result.failure_context

    script = tmp_path / "slow_runner.py"
    script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")

    class CancelOnStartExecutor(ScriptExecutor):
        def _start_process(self, command, environment):
            process = super()._start_process(command, environment)
            self.cancel("cancel-on-start")
            return process

    early_cancel = CancelOnStartExecutor(tmp_path, script, timeout_seconds=5)
    early_result = early_cancel.execute(
        router.config.spec_for(ModelTier.LUNA),
        router.begin("cancel-on-start").decision,
    )
    assert early_result.outcome is AttemptOutcome.FAILURE
    assert "stopped" in early_result.failure_context

    running = ScriptExecutor(tmp_path, script, timeout_seconds=5)
    holder: dict[str, ModelExecution] = {}

    def execute() -> None:
        holder["result"] = running.execute(
            router.config.spec_for(ModelTier.LUNA),
            router.begin("cancel-running").decision,
        )

    thread = Thread(target=execute)
    thread.start()
    deadline = time.monotonic() + 3
    while "cancel-running" not in running._processes:
        if time.monotonic() >= deadline:
            raise AssertionError("The test process did not start")
        time.sleep(0.01)
    running.cancel("cancel-running")
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert holder["result"].outcome is AttemptOutcome.FAILURE
    assert "stopped" in holder["result"].failure_context


def test_executor_handles_nonzero_and_empty_output(tmp_path: Path) -> None:
    router = ModelRouter(RoutingStore())
    failed_script = tmp_path / "failed_runner.py"
    failed_script.write_text(
        "print('token=visible-secret')\nraise SystemExit(3)\n",
        encoding="utf-8",
    )
    failed = ScriptExecutor(tmp_path, failed_script).execute(
        router.config.spec_for(ModelTier.LUNA),
        router.begin("nonzero").decision,
    )
    assert failed.outcome is AttemptOutcome.FAILURE
    assert "Bounded agent failed" in failed.failure_context
    assert "token=[redacted]" in failed.failure_context

    empty_script = tmp_path / "empty_runner.py"
    empty_script.write_text("pass\n", encoding="utf-8")
    empty = ScriptExecutor(tmp_path, empty_script).execute(
        router.config.spec_for(ModelTier.LUNA),
        router.begin("empty").decision,
    )
    assert empty.outcome is AttemptOutcome.FAILURE
    assert "no result" in empty.failure_context

    silent_script = tmp_path / "silent_failure.py"
    silent_script.write_text("raise SystemExit(4)\n", encoding="utf-8")
    silent = ScriptExecutor(tmp_path, silent_script).execute(
        router.config.spec_for(ModelTier.LUNA),
        router.begin("silent-failure").decision,
    )
    assert silent.outcome is AttemptOutcome.FAILURE
    assert "status 4" in silent.failure_context


def test_executor_parses_fallback_messages_and_invalid_usage() -> None:
    parsed = CodexExecModelExecutor._parse_output(
        "\n".join(
            (
                "not json",
                json.dumps(["not an event"]),
                json.dumps(
                    {
                        "type": "agent_message",
                        "content": [
                            {"text": "first"},
                            "ignored",
                            {"text": 3},
                            {"text": "second"},
                        ],
                        "usage": {"input_tokens": -1, "output_tokens": True},
                    }
                ),
            )
        )
    )
    assert parsed[0] == "first\nsecond"
    assert parsed[1] >= 1
    assert parsed[2] >= 1
