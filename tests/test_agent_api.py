from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response

from beehaiive.agents import AgentService
from beehaiive.app import create_app
from beehaiive.database import SnapshotDatabase
from beehaiive.snapshot import ProjectSnapshotService
from beehaiive.workflows import WorkflowStore
from tests.agent_support import (
    REPOSITORY,
    FakeCodexProcess,
    StaticProvider,
    init_checkout,
    todo_snapshot,
    workflow_definition,
)
from tests.support import skills_config


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    return init_checkout(tmp_path / "agent-checkout")


@pytest.fixture
def commands() -> list[list[str]]:
    return []


@pytest.fixture
def client(tmp_path: Path, commands: list[list[str]]) -> Iterator[TestClient]:
    skill = tmp_path / "deliver" / "SKILL.md"
    skill.parent.mkdir()
    skill.write_text("deliver", encoding="utf-8")
    database = SnapshotDatabase(":memory:")
    provider = StaticProvider(todo_snapshot())
    service = ProjectSnapshotService(provider, database, minimum_refresh_seconds=0)
    config = skills_config(tmp_path, agent_step_timeout_seconds=5)

    def process_factory(command: list[str], **kwargs: Any) -> FakeCodexProcess:
        del kwargs
        commands.append(command)
        return FakeCodexProcess(command)

    store = WorkflowStore(database)
    agents = AgentService(
        config, database, store, service, process_factory=process_factory
    )
    app = create_app(config, database=database, service=service, agent_service=agents)
    with TestClient(app) as test_client:
        yield test_client
    database.close()


@pytest.fixture
def created_workflow(client: TestClient) -> Response:
    body = {"definition": workflow_definition(), "source_prompt": "Wait."}
    created_workflow = client.post("/api/workflows", json=body)
    assert created_workflow.status_code == 200
    return created_workflow


def create_agent(
    client: TestClient,
    workflow: Response,
    name: str,
    checkout_path: Path,
    **extra: Any,
) -> Response:
    payload: dict[str, Any] = {
        "name": name,
        "workflow_id": workflow.json()["id"],
        "parameters": {},
        "repository": REPOSITORY,
        "checkout_path": str(checkout_path),
    }
    return client.post("/api/agents", json=payload | extra)


def await_waiting_agent(client: TestClient, agent_id: str) -> dict[str, Any]:
    current: dict[str, Any] = {}
    for _ in range(100):
        current = client.get(f"/api/agents/{agent_id}").json()
        if current["status"] == "waiting" and current["history"]:
            break
        time.sleep(0.01)
    return current


@pytest.fixture
def agent_id(client: TestClient, created_workflow: Response, checkout: Path) -> str:
    created = create_agent(
        client, created_workflow, "delivery-agent", checkout, model="gpt-5.6-luna"
    )
    assert created.status_code == 200, created.text
    started = client.post(f"/api/agents/{created.json()['id']}/start")
    assert started.status_code == 200, started.text
    await_waiting_agent(client, created.json()["id"])
    return str(created.json()["id"])


@pytest.fixture
def second_agent(
    client: TestClient, created_workflow: Response, tmp_path: Path
) -> Response:
    second_agent = create_agent(
        client, created_workflow, "second-agent", tmp_path / "second-checkout"
    )
    assert second_agent.status_code == 200, second_agent.text
    return second_agent


def test_agent_api_records_one_completed_pass_for_the_skill_state(
    client: TestClient, agent_id: str
) -> None:
    current = client.get(f"/api/agents/{agent_id}").json()

    assert current["status"] == "waiting"
    assert current["history"][0]["status"] == "completed"
    assert current["history"][0]["steps"][1]["outcome"] == "done"
    assert current["history"][0]["steps"][1]["exit_code"] == 0


def test_agent_api_runs_one_fresh_ephemeral_codex_process(
    client: TestClient, commands: list[list[str]], agent_id: str
) -> None:
    current = client.get(f"/api/agents/{agent_id}").json()

    assert len(commands) == 1
    assert commands[0][:2] == ["codex", "exec"]
    assert commands[0] == current["history"][0]["steps"][1]["command"]
    assert "--ephemeral" in commands[0]
    assert "--output-schema" in commands[0]
    assert "--json" in commands[0]
    assert commands[0][-1] == "-"


def test_agent_api_logs_long_stdout_and_stderr_events(
    client: TestClient, agent_id: str
) -> None:
    log_lines = client.get(f"/api/agents/{agent_id}/logs").json()["lines"]

    assert any(len(line) > 20_000 for line in log_lines)
    assert any("stderr event" in line for line in log_lines)


@pytest.mark.usefixtures("agent_id")
def test_agent_api_board_does_not_expose_the_holder(client: TestClient) -> None:
    board = client.get("/api/project")

    assert board.status_code == 200
    assert "holder" not in board.json()["columns"][0]["items"][0]


@pytest.mark.usefixtures("agent_id")
def test_agent_api_rejects_a_second_agent_on_the_same_checkout(
    client: TestClient, created_workflow: Response, checkout: Path
) -> None:
    duplicate_checkout = create_agent(
        client, created_workflow, "duplicate-checkout", checkout
    )

    assert duplicate_checkout.status_code == 409


@pytest.mark.usefixtures("agent_id")
def test_agent_api_rejects_moving_an_agent_onto_a_used_checkout(
    client: TestClient, second_agent: Response, checkout: Path
) -> None:
    update_conflict = client.patch(
        f"/api/agents/{second_agent.json()['id']}",
        json={"checkout_path": str(checkout)},
    )

    assert update_conflict.status_code == 409


def test_agent_api_deletes_an_agent_that_never_ran(
    client: TestClient, second_agent: Response
) -> None:
    assert client.delete(f"/api/agents/{second_agent.json()['id']}").status_code == 200


def test_agent_api_refuses_to_delete_an_agent_or_workflow_in_use(
    client: TestClient, created_workflow: Response, agent_id: str
) -> None:
    assert (
        client.delete(f"/api/workflows/{created_workflow.json()['id']}").status_code
        == 409
    )
    assert client.delete(f"/api/agents/{agent_id}").status_code == 409


def test_agent_api_stop_makes_the_agent_deletable(
    client: TestClient, agent_id: str
) -> None:
    assert client.post(f"/api/agents/{agent_id}/stop").status_code == 200
    assert client.delete(f"/api/agents/{agent_id}").status_code == 200
