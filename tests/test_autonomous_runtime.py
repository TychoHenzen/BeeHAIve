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

    def launch(_command, _environment):
        raise FileNotFoundError(2, "The system cannot find the file specified", missing)

    monkeypatch.setattr(
        autonomous.CodexProcessMixin, "_start_process", staticmethod(launch)
    )

    with pytest.raises(RuntimeError) as error:
        CodexSkillExecutor(tmp_path, str(missing)).execute(
            SkillStep("runtime", str(skill_path), "Exercise diagnostics"),
            {"repository": "owner/api"},
            {},
        )

    message = str(error.value)
    assert "Codex launch failed" in message
    assert "missing-codex.exe" in message
    assert "errno=2" in message
    assert "cwd=" in message


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
