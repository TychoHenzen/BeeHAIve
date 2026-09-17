from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

import beehaiive.autonomous as autonomous
from beehaiive.autonomous import (
    AutonomousLifecycleService,
    CodexSkillExecutor,
    SkillStep,
)
from beehaiive.orchestrator import Orchestrator
from beehaiive.storage import OrchestratorStore
from tests.conftest import FakeProvider
from tests.support.dashboard.helpers import dashboard_snapshot


def test_autonomous_runtime_resolves_bare_codex_before_windows_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill_path = tmp_path / "skill" / "SKILL.md"
    skill_path.parent.mkdir()
    skill_path.write_text("# test skill\n", encoding="utf-8")
    resolved_executable = tmp_path / "codex.exe"
    resolved_executable.write_bytes(b"native executable placeholder")
    launches: list[list[str]] = []

    monkeypatch.setenv("BEEHAIIVE_AUTONOMOUS_MODE", "codex")
    monkeypatch.setenv("BEEHAIIVE_AGENT_REPOSITORY", str(tmp_path))
    monkeypatch.setenv("BEEHAIIVE_AGENT_REPOSITORY_NAME", "owner/api")
    monkeypatch.setenv("BEEHAIIVE_CODEX_EXECUTABLE", "codex")
    monkeypatch.setattr(
        shutil,
        "which",
        lambda command: str(resolved_executable) if command == "codex.exe" else None,
    )

    def launch(command: list[str], _environment: dict[str, str]):
        launches.append(command)
        return subprocess.Popen(
            [
                sys.executable,
                "-c",
                "print('{\"status\":\"succeeded\",\"summary\":\"launched\"}')",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    monkeypatch.setattr(
        autonomous.CodexProcessMixin, "_start_process", staticmethod(launch)
    )
    step = SkillStep("runtime", str(skill_path), "Exercise the runtime launch")
    monkeypatch.setattr(autonomous, "AUTONOMOUS_STEPS", (step,))

    store = OrchestratorStore()
    service = Orchestrator(store, FakeProvider(dashboard_snapshot()))
    service.synchronize("project-1")
    automation = AutonomousLifecycleService(
        service, CodexSkillExecutor(tmp_path, "codex")
    )
    try:
        started = automation.start("project-1", "owner/api", 1)
        deadline = time.monotonic() + 3
        current = automation.status(str(started["run_id"]))
        while current["status"] == "running" and time.monotonic() < deadline:
            time.sleep(0.01)
            current = automation.status(str(started["run_id"]))

        assert current["status"] == "completed", {
            "run_status": current["status"],
            "run_error": current.get("error"),
            "launched_executable": launches[0][0] if launches else None,
        }
        assert launches[0][0] == str(resolved_executable)
        assert launches[0][launches[0].index("--model") + 1] == "gpt-5.6-luna"
        assert "scheduler already assigned PBI #1" in launches[0][-1]
        assert "internal handover after doing the stage work" in launches[0][-1]
    finally:
        store.close()


def test_autonomous_runtime_reports_launch_context_for_missing_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill_path = tmp_path / "skill" / "SKILL.md"
    skill_path.parent.mkdir()
    skill_path.write_text("# test skill\n", encoding="utf-8")
    missing = tmp_path / "missing-codex.exe"
    process_events: list[dict[str, object]] = []

    def launch(_command, _environment):
        raise FileNotFoundError(2, "The system cannot find the file specified", missing)

    monkeypatch.setattr(
        autonomous.CodexProcessMixin, "_start_process", staticmethod(launch)
    )

    with pytest.raises(RuntimeError) as error:
        CodexSkillExecutor(
            tmp_path, str(missing), on_process=process_events.append
        ).execute(
            SkillStep("runtime", str(skill_path), "Exercise diagnostics"),
            {"repository": "owner/api"},
            {},
        )

    message = str(error.value)
    assert "Codex launch failed" in message
    assert "missing-codex.exe" in message
    assert "errno=2" in message
    assert "cwd=" in message
    assert process_events[0]["state"] == "launch_failed"
    assert "missing-codex.exe" in str(process_events[0]["error"])


def test_autonomous_runtime_accepts_pretty_printed_json_handover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill_path = tmp_path / "skill" / "SKILL.md"
    skill_path.parent.mkdir()
    skill_path.write_text("# test skill\n", encoding="utf-8")
    outputs: list[str] = []
    script = (
        "import sys\n"
        "print('progress', file=sys.stderr)\n"
        "print('log')\n"
        "print('{\"status\":\"succeeded\",\"summary\":\"pretty\", "
        "\"handover\":{\"step\":\"next\"}}')\n"
    )

    def launch(_command, _environment):
        return subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    monkeypatch.setattr(
        autonomous.CodexProcessMixin, "_start_process", staticmethod(launch)
    )

    result = CodexSkillExecutor(
        tmp_path, "codex.exe", on_output=outputs.append
    ).execute(
        SkillStep("runtime", str(skill_path), "Exercise JSON parsing"),
        {"repository": "owner/api"},
        {},
    )

    assert result["status"] == "succeeded"
    assert result["handover"] == {"step": "next"}
    assert "log" in result["session_output"]
    assert '"status":"succeeded"' in result["session_output"]
    assert any("progress" in output for output in outputs)
    assert any("log" in output for output in outputs)


def test_autonomous_runtime_reports_process_activity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill_path = tmp_path / "skill" / "SKILL.md"
    skill_path.parent.mkdir()
    skill_path.write_text("# test skill\n", encoding="utf-8")
    process_events: list[dict[str, object]] = []

    def launch(_command, _environment):
        return subprocess.Popen(
            [
                sys.executable,
                "-c",
                "print('{\"status\":\"succeeded\",\"summary\":\"done\"}', flush=True)",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    monkeypatch.setattr(
        autonomous.CodexProcessMixin, "_start_process", staticmethod(launch)
    )

    result = CodexSkillExecutor(
        tmp_path, "codex.exe", on_process=process_events.append
    ).execute(
        SkillStep("runtime", str(skill_path), "Exercise process diagnostics"),
        {"repository": "owner/api"},
        {},
    )

    assert result["status"] == "succeeded"
    assert [event["state"] for event in process_events] == ["running", "exited"]
    assert process_events[0]["pid"]
    assert process_events[0]["cwd"] == str(tmp_path.resolve())
    assert process_events[0]["timeout_seconds"] is None
    assert process_events[1]["returncode"] == 0


def test_autonomous_runtime_timeout_preserves_live_session_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill_path = tmp_path / "skill" / "SKILL.md"
    skill_path.parent.mkdir()
    skill_path.write_text("# test skill\n", encoding="utf-8")
    script = "import time; print('doing work', flush=True); time.sleep(2)"

    def launch(_command, _environment):
        return subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    monkeypatch.setattr(
        autonomous.CodexProcessMixin, "_start_process", staticmethod(launch)
    )

    with pytest.raises(RuntimeError, match="Codex timed out after 0.2s") as error:
        CodexSkillExecutor(tmp_path, "codex.exe", timeout_seconds=0.2).execute(
            SkillStep("runtime", str(skill_path), "Exercise timeout diagnostics"),
            {"repository": "owner/api"},
            {},
        )

    assert "doing work" in str(error.value)


def test_autonomous_timeout_is_unlimited_unless_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BEEHAIIVE_AUTONOMOUS_TIMEOUT_SECONDS", raising=False)
    assert autonomous._autonomous_timeout() is None
    monkeypatch.setenv("BEEHAIIVE_AUTONOMOUS_TIMEOUT_SECONDS", "0")
    assert autonomous._autonomous_timeout() is None
    monkeypatch.setenv("BEEHAIIVE_AUTONOMOUS_TIMEOUT_SECONDS", "12")
    assert autonomous._autonomous_timeout() == 12
