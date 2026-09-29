from __future__ import annotations

import io
import json
import subprocess
import time
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from beehaiive.core.agents import (
    AgentService,
    AgentStore,
    _remote_repository,
    _skill_prompt,
)
from beehaiive.core.app import create_app
from beehaiive.core.config import CoreConfig
from beehaiive.core.database import SnapshotDatabase
from beehaiive.core.models import ProjectCard, ProjectColumn, ProjectSnapshot
from beehaiive.core.snapshot import ProjectSnapshotService
from beehaiive.core.workflows import WorkflowStore


class StaticProvider:
    def __init__(self, snapshot: ProjectSnapshot) -> None:
        self.snapshot = snapshot

    def fetch_snapshot(self) -> ProjectSnapshot:
        return self.snapshot

    def fetch_item(
        self,
        item_key: str,
        *,
        status_field_id: str | None,
        status_options: tuple[str, ...],
    ) -> tuple[ProjectCard, str]:
        del status_field_id, status_options
        for column in self.snapshot.columns:
            for card in column.items:
                if card.item_key == item_key:
                    return card, column.status
        raise AssertionError(f"missing fake Project item {item_key}")


class FakeCodexProcess:
    def __init__(self, command: list[str], **_kwargs: Any) -> None:
        self.command = command
        self.returncode = 0
        self.pid = None

    def communicate(self, _prompt: str, timeout: float) -> tuple[str, str]:
        del timeout
        result_path = Path(self.command[self.command.index("-o") + 1])
        result_path.write_text(
            json.dumps(
                {
                    "outcome": "done",
                    "summary": "completed the held item",
                    "handover": {"evidence": "fake process"},
                }
            ),
            encoding="utf-8",
        )
        return "event " + ("x" * 20_000), "stderr event"

    def terminate(self) -> None:
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9


class StreamingProcess:
    def __init__(self) -> None:
        self.stdout = io.StringIO("stdout first\nstdout second\n")
        self.stderr = io.StringIO("stderr first\n")
        self.stdin = RecordingStdin()
        self.returncode = 7

    def wait(self, *, timeout: float) -> int:
        del timeout
        return self.returncode


class RecordingStdin:
    def __init__(self) -> None:
        self.value = ""

    def write(self, value: str) -> None:
        self.value += value

    def close(self) -> None:
        pass


def test_streaming_process_keeps_stdout_stderr_and_exit_code(tmp_path: Path) -> None:
    process = StreamingProcess()
    log_path = tmp_path / "step.jsonl"

    result = AgentService._communicate_process(
        AgentService.__new__(AgentService),
        process,
        "prompt",
        log_path,
        timeout=1,
    )

    assert result == 7
    assert process.stdin.value == "prompt"
    events = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert {(event["event"], event["line"]) for event in events} == {
        ("stdout", "stdout first"),
        ("stdout", "stdout second"),
        ("stderr", "stderr first"),
    }


def test_remote_repository_accepts_github_urls_only() -> None:
    assert _remote_repository("https://github.com/Owner/Repo.git") == "Owner/Repo"
    assert _remote_repository("git@github.com:Owner/Repo.git") == "Owner/Repo"
    assert _remote_repository("https://gitlab.com/Owner/Repo.git") is None
    assert _remote_repository("") is None


def workflow_definition() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "name": "Delivery",
        "description": "Run one skill.",
        "source_prompt": "Wait for Todo.",
        "auto_reset_on_stall": False,
        "max_steps_per_pass": 4,
        "parameters": [
            {"name": "status", "type": "status", "const": True, "value": "Todo"}
        ],
        "initial": "wait",
        "states": [
            {
                "id": "wait",
                "title": "Wait",
                "action": "wait_for_work",
                "max_visits": 1,
            },
            {
                "id": "run",
                "title": "Run",
                "action": "run_skill",
                "skill": "deliver",
                "prompt": "Deliver the held item.",
                "outcomes": ["done"],
                "max_visits": 1,
            },
        ],
        "transitions": [
            {
                "from": "wait",
                "to": "run",
                "priority": 1,
                "conditions": [{"kind": "item_status_is", "value": "{status}"}],
            },
            {
                "from": "run",
                "to": "wait",
                "priority": 1,
                "conditions": [{"kind": "outcome_is", "value": "done"}],
            },
        ],
    }


def test_agent_api_runs_one_fresh_codex_process_per_skill_state(tmp_path: Path) -> None:
    skill = tmp_path / "deliver" / "SKILL.md"
    skill.parent.mkdir()
    skill.write_text("deliver", encoding="utf-8")
    checkout = tmp_path / "agent-checkout"
    checkout.mkdir()
    subprocess.run(["git", "init", str(checkout)], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(checkout),
            "remote",
            "add",
            "origin",
            "https://github.com/TychoHenzen/BeeHAIve.git",
        ],
        check=True,
        capture_output=True,
    )
    snapshot = ProjectSnapshot(
        fetched_at="2026-09-29T10:00:00+00:00",
        rate_limited_until=None,
        columns=(
            ProjectColumn(
                status="Todo",
                items=(
                    ProjectCard(
                        type="Issue",
                        repository="TychoHenzen/BeeHAIve",
                        number=229,
                        title="Run assigned workflow",
                        url="https://github.com/TychoHenzen/BeeHAIve/issues/229",
                        state="open",
                        labels=(),
                        linked_issue_numbers=(),
                        item_key="project-item-229",
                    ),
                ),
            ),
        ),
    )
    database = SnapshotDatabase(":memory:")
    provider = StaticProvider(snapshot)
    service = ProjectSnapshotService(provider, database, minimum_refresh_seconds=0)
    config = CoreConfig(
        owner="TychoHenzen",
        owner_type="user",
        project_number=2,
        github_token="token",
        skills_dirs=(tmp_path,),
        agent_step_timeout_seconds=5,
    )
    commands: list[list[str]] = []

    def process_factory(command: list[str], **kwargs: Any) -> FakeCodexProcess:
        del kwargs
        commands.append(command)
        return FakeCodexProcess(command)

    from beehaiive.core.agents import AgentService

    agents = AgentService(
        config,
        database,
        WorkflowStore(database),
        service,
        process_factory=process_factory,
    )
    app = create_app(
        config,
        database=database,
        service=service,
        agent_service=agents,
    )
    with TestClient(app) as client:
        created_workflow = client.post(
            "/api/workflows",
            json={"definition": workflow_definition(), "source_prompt": "Wait."},
        )
        assert created_workflow.status_code == 200
        created = client.post(
            "/api/agents",
            json={
                "name": "delivery-agent",
                "workflow_id": created_workflow.json()["id"],
                "parameters": {},
                "repository": "TychoHenzen/BeeHAIve",
                "checkout_path": str(checkout),
                "model": "gpt-5.6-luna",
            },
        )
        assert created.status_code == 200, created.text
        agent_id = created.json()["id"]
        started = client.post(f"/api/agents/{agent_id}/start")
        assert started.status_code == 200, started.text
        for _ in range(100):
            current = client.get(f"/api/agents/{agent_id}").json()
            if current["status"] == "waiting" and current["history"]:
                break
            time.sleep(0.01)
        assert current["status"] == "waiting"
        assert current["history"][0]["status"] == "completed"
        assert current["history"][0]["steps"][1]["outcome"] == "done"
        assert current["history"][0]["steps"][1]["exit_code"] == 0
        log_lines = client.get(f"/api/agents/{agent_id}/logs").json()["lines"]
        assert any(len(line) > 20_000 for line in log_lines)
        assert any("stderr event" in line for line in log_lines)
        duplicate_checkout = client.post(
            "/api/agents",
            json={
                "name": "duplicate-checkout",
                "workflow_id": created_workflow.json()["id"],
                "parameters": {},
                "repository": "TychoHenzen/BeeHAIve",
                "checkout_path": str(checkout),
            },
        )
        assert duplicate_checkout.status_code == 409
        second_agent = client.post(
            "/api/agents",
            json={
                "name": "second-agent",
                "workflow_id": created_workflow.json()["id"],
                "parameters": {},
                "repository": "TychoHenzen/BeeHAIve",
                "checkout_path": str(tmp_path / "second-checkout"),
            },
        )
        assert second_agent.status_code == 200, second_agent.text
        update_conflict = client.patch(
            f"/api/agents/{second_agent.json()['id']}",
            json={"checkout_path": str(checkout)},
        )
        assert update_conflict.status_code == 409
        assert (
            client.delete(f"/api/agents/{second_agent.json()['id']}").status_code == 200
        )
        assert (
            client.delete(f"/api/workflows/{created_workflow.json()['id']}").status_code
            == 409
        )
        assert client.delete(f"/api/agents/{agent_id}").status_code == 409
        assert client.post(f"/api/agents/{agent_id}/stop").status_code == 200
        assert client.delete(f"/api/agents/{agent_id}").status_code == 200
        board = client.get("/api/project")

    assert board.status_code == 200
    assert "holder" not in board.json()["columns"][0]["items"][0]
    assert len(commands) == 1
    assert commands[0][:2] == ["codex", "exec"]
    assert commands[0] == current["history"][0]["steps"][1]["command"]
    assert "--ephemeral" in commands[0]
    assert "--output-schema" in commands[0]
    assert "--json" in commands[0]
    assert commands[0][-1] == "-"
    database.close()


def test_agent_claim_is_unique_and_stop_releases_it() -> None:
    database = SnapshotDatabase(":memory:")
    store = AgentStore(database)
    first = store.create(
        name="first",
        workflow_id=1,
        workflow_revision=1,
        parameters={},
        repository="TychoHenzen/BeeHAIve",
        checkout_path="C:/agents/first",
        model=None,
    )
    second = store.create(
        name="second",
        workflow_id=1,
        workflow_revision=1,
        parameters={},
        repository="TychoHenzen/BeeHAIve",
        checkout_path="C:/agents/second",
        model=None,
    )
    card = ProjectCard(
        type="Issue",
        repository="TychoHenzen/BeeHAIve",
        number=1,
        title="one",
        url="https://github.com/TychoHenzen/BeeHAIve/issues/1",
        state="open",
        labels=(),
        linked_issue_numbers=(),
        item_key="item-1",
    )
    store.set_status(first["id"], "waiting")
    store.set_status(second["id"], "waiting")
    first_pass = store.claim(
        first["id"],
        card,
        status="Todo",
        workflow_id=1,
        workflow_revision=1,
        initial_state="wait",
    )
    assert first_pass is not None
    assert (
        store.claim(
            second["id"],
            card,
            status="Todo",
            workflow_id=1,
            workflow_revision=1,
            initial_state="wait",
        )
        is None
    )
    store.stop(first["id"])
    second_pass = store.claim(
        second["id"],
        card,
        status="Todo",
        workflow_id=1,
        workflow_revision=1,
        initial_state="wait",
    )
    assert second_pass is not None
    assert store.holders()["item-1"]["id"] == second["id"]
    store.stop(second["id"])
    database.close()


def test_agent_stop_and_restart_recovery_close_running_steps(tmp_path: Path) -> None:
    database = SnapshotDatabase(":memory:")
    store = AgentStore(database)
    agent = store.create(
        name="runner",
        workflow_id=1,
        workflow_revision=1,
        parameters={},
        repository="TychoHenzen/BeeHAIve",
        checkout_path=str(tmp_path / "runner"),
        model=None,
    )
    store.set_status(agent["id"], "waiting")
    card = ProjectCard(
        type="Issue",
        repository="TychoHenzen/BeeHAIve",
        number=229,
        title="runner",
        url="https://github.com/TychoHenzen/BeeHAIve/issues/229",
        state="open",
        labels=(),
        linked_issue_numbers=(),
        item_key="item-229",
    )
    pass_id = store.claim(
        agent["id"],
        card,
        status="Todo",
        workflow_id=1,
        workflow_revision=1,
        initial_state="wait",
    )
    assert pass_id is not None
    sequence = store.next_step_sequence(pass_id)
    log_path = store.log_path(str(tmp_path / "runner"), pass_id, sequence)
    store.begin_step(pass_id, "run", "run_skill", ["codex", "exec"], str(log_path))
    store.stop(agent["id"])
    stopped = store.get(agent["id"])
    assert stopped is not None
    assert stopped["history"][0]["status"] == "stopped"
    assert stopped["history"][0]["steps"][0]["status"] == "stopped"

    second_database = SnapshotDatabase(":memory:")
    second_store = AgentStore(second_database)
    second_agent = second_store.create(
        name="restarted-runner",
        workflow_id=1,
        workflow_revision=1,
        parameters={},
        repository="TychoHenzen/BeeHAIve",
        checkout_path=str(tmp_path / "restarted"),
        model=None,
    )
    second_store.set_status(second_agent["id"], "waiting")
    second_pass = second_store.claim(
        second_agent["id"],
        card,
        status="Todo",
        workflow_id=1,
        workflow_revision=1,
        initial_state="wait",
    )
    assert second_pass is not None
    second_sequence = second_store.next_step_sequence(second_pass)
    second_log = second_store.log_path(
        str(tmp_path / "restarted"), second_pass, second_sequence
    )
    second_store.begin_step(
        second_pass, "run", "run_skill", ["codex", "exec"], str(second_log)
    )
    AgentStore(second_database).recover_abandoned()
    recovered = second_store.get(second_agent["id"])
    assert recovered is not None
    assert recovered["status"] == "stopped"
    assert recovered["history"][0]["status"] == "interrupted"
    assert recovered["history"][0]["steps"][0]["status"] == "interrupted"
    second_database.close()
    database.close()


def test_skill_prompt_exposes_real_skill_path_and_child_authority(
    tmp_path: Path,
) -> None:
    skill_path = tmp_path / "deliver" / "SKILL.md"
    skill_path.parent.mkdir()
    skill_path.write_text("deliver", encoding="utf-8")
    card = ProjectCard(
        type="Issue",
        repository="TychoHenzen/BeeHAIve",
        number=229,
        title="runner",
        url=None,
        state="open",
        labels=(),
        linked_issue_numbers=(),
    )

    prompt = _skill_prompt(
        {"skill": "deliver", "prompt": "Work the item.", "outcomes": ["done"]},
        card,
        "Todo",
        {},
        skills_dirs=(tmp_path,),
    )

    assert str(skill_path.resolve()) in prompt
    assert "authorized gh/git workflow" in prompt
    assert "Return blocked only when authority" in prompt
