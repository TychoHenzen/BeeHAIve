from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from beehaiive.core.agents import AgentStore
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
        return "", ""

    def terminate(self) -> None:
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9


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
