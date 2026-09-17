from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

import pytest

import beehaiive.autonomous as autonomous
from beehaiive.autonomous import (
    AutonomousLifecycleService,
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
    resolved_executable = tmp_path / "codex.cmd"
    resolved_executable.write_text("@echo off\n", encoding="utf-8")
    launches: list[list[str]] = []

    monkeypatch.setenv("BEEHAIIVE_AUTONOMOUS_MODE", "codex")
    monkeypatch.setenv("BEEHAIIVE_AGENT_REPOSITORY", str(tmp_path))
    monkeypatch.setenv("BEEHAIIVE_AGENT_REPOSITORY_NAME", "owner/api")
    monkeypatch.setenv("BEEHAIIVE_CODEX_EXECUTABLE", "codex")
    monkeypatch.setattr(shutil, "which", lambda _command: str(resolved_executable))

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        launches.append(command)
        if command[0] == "codex":
            raise FileNotFoundError(
                "[WinError 2] The system cannot find the file specified"
            )
        return subprocess.CompletedProcess(
            command,
            0,
            '{"status":"succeeded","summary":"launched"}\n',
            "",
        )

    monkeypatch.setattr(autonomous.subprocess, "run", run)
    step = SkillStep("runtime", str(skill_path), "Exercise the runtime launch")
    monkeypatch.setattr(autonomous, "AUTONOMOUS_STEPS", (step,))

    store = OrchestratorStore()
    service = Orchestrator(store, FakeProvider(dashboard_snapshot()))
    service.synchronize("project-1")
    automation = AutonomousLifecycleService(service)
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
    finally:
        store.close()
