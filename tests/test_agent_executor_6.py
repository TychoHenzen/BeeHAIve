import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import beehaiive.agent as agent_module
from beehaiive.agent import (
    CodexExecModelExecutor,
)


def test_executor_builds_model_command_and_covers_process_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor = CodexExecModelExecutor(tmp_path, model="model-x")
    command = executor._command("prompt")
    assert command[-3:] == ["--model", "model-x", "prompt"]
    assert "--sandbox" in command
    assert "--skip-git-repo-check" in command
    assert "--ignore-user-config" in command
    assert "--ignore-rules" in command
    assert "--approve-for-me" in command
    assert executor._command("prompt")[-1] == "prompt"

    captured: dict[str, object] = {}

    class StartedProcess:
        pass

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return StartedProcess()

    monkeypatch.setattr(agent_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(agent_module.os, "name", "posix")
    started = CodexExecModelExecutor._start_process(
        [sys.executable, "-c", "pass", "--cd", str(tmp_path)], {}
    )
    assert isinstance(started, StartedProcess)
    assert captured["start_new_session"] is True

    monkeypatch.setattr(agent_module.os, "name", "nt")
    monkeypatch.setattr(
        agent_module.subprocess,
        "CREATE_NEW_PROCESS_GROUP",
        0x200,
        raising=False,
    )
    captured.pop("start_new_session")
    started = CodexExecModelExecutor._start_process(
        [sys.executable, "-c", "pass", "--cd", str(tmp_path)], {}
    )
    assert isinstance(started, StartedProcess)
    assert captured["creationflags"] == 0x200
    assert "start_new_session" not in captured
    monkeypatch.setattr(agent_module.os, "name", "posix")
    monkeypatch.setattr(agent_module.signal, "SIGTERM", 15, raising=False)
    monkeypatch.setattr(agent_module.signal, "SIGKILL", 9, raising=False)

    class Process:
        pid = 2

        def __init__(self, running: bool = True) -> None:
            self.running = running
            self.terminated = False
            self.killed = False
            self.waits = 0

        def poll(self):
            return None if self.running else 0

        def terminate(self):
            self.terminated = True
            self.running = False

        def kill(self):
            self.killed = True
            self.running = False

        def wait(self, timeout=None):
            del timeout
            self.waits += 1
            self.running = False

    clock_value = [0.0]

    def monotonic() -> float:
        current = clock_value[0]
        clock_value[0] += 0.5
        return current

    def sleep(seconds: float) -> None:
        clock_value[0] += seconds

    monkeypatch.setattr(agent_module.time, "monotonic", monotonic)
    monkeypatch.setattr(agent_module.time, "sleep", sleep)
    process_group_alive = [True]
    group_signals: list[int] = []

    def kill_group(group: int, sig: int) -> None:
        assert group == 2
        if sig == 0:
            if not process_group_alive[0]:
                raise ProcessLookupError("process group exited")
            return
        group_signals.append(sig)
        if sig == 9:
            process_group_alive[0] = False

    monkeypatch.setattr(agent_module.os, "killpg", kill_group, raising=False)
    leader_exited = Process()
    CodexExecModelExecutor._terminate_process(leader_exited)
    assert group_signals == [15, 9]
    assert not process_group_alive[0]
    assert not leader_exited.terminated

    def missing_group(_group: int, _sig: int) -> None:
        raise ProcessLookupError("process group exited")

    monkeypatch.setattr(agent_module.os, "killpg", missing_group, raising=False)
    missing_group_process = Process()
    CodexExecModelExecutor._terminate_process(missing_group_process)
    assert missing_group_process.terminated

    finished_process = Process(running=False)
    CodexExecModelExecutor._terminate_process(finished_process)
    assert finished_process.waits == 0

    clock_value[0] = 0
    stubborn_group_alive = [True]

    def stubborn_group(_group: int, sig: int) -> None:
        if sig == 0:
            if not stubborn_group_alive[0]:
                raise ProcessLookupError("process group exited")
        elif sig == 9:
            stubborn_group_alive[0] = False

    monkeypatch.setattr(agent_module.os, "killpg", stubborn_group, raising=False)

    class StuckProcess(Process):
        def wait(self, timeout=None):
            self.waits += 1
            raise subprocess.TimeoutExpired("process", timeout)

    stuck_process = StuckProcess()
    CodexExecModelExecutor._terminate_process(stuck_process)
    assert stuck_process.killed
    assert stuck_process.waits == 4

    monkeypatch.setattr(agent_module.os, "name", "nt")
    taskkill_calls = []

    def taskkill(command, **kwargs):
        taskkill_calls.append((command, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(agent_module.subprocess, "run", taskkill)
    windows_process = Process()
    CodexExecModelExecutor._terminate_process(windows_process)
    assert taskkill_calls[0][0] == ["taskkill", "/PID", "2", "/T", "/F"]
    assert taskkill_calls[0][1]["check"] is False
    assert windows_process.waits == 1

    class WindowsProcessTimeoutOnce(Process):
        def wait(self, timeout=None):
            self.waits += 1
            if self.waits == 1:
                raise subprocess.TimeoutExpired("process", timeout)
            self.running = False

    taskkill_calls.clear()
    timeout_windows_process = WindowsProcessTimeoutOnce()
    CodexExecModelExecutor._terminate_process(timeout_windows_process)
    assert len(taskkill_calls) == 2
    assert timeout_windows_process.waits == 2

    finished_windows_process = Process(running=False)
    CodexExecModelExecutor._terminate_process(finished_windows_process)
    assert finished_windows_process.waits == 0

    def failed_taskkill(command, **kwargs):
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(agent_module.subprocess, "run", failed_taskkill)
    failed_windows_process = Process()
    CodexExecModelExecutor._terminate_process(failed_windows_process)
    assert failed_windows_process.waits == 1
    assert not failed_windows_process.killed
