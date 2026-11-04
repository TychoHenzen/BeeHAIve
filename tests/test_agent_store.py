from __future__ import annotations

from pathlib import Path

from beehaiive.agents import AgentStore
from beehaiive.database import SnapshotDatabase
from tests.agent_support import (
    begin_run_step,
    claim_todo,
    issue_card,
    waiting_agent,
)


def test_agent_claim_is_unique_and_stop_releases_it() -> None:
    database = SnapshotDatabase(":memory:")
    store = AgentStore(database)
    first = waiting_agent(store, "first", "C:/agents/first")
    second = waiting_agent(store, "second", "C:/agents/second")
    card = issue_card(1, "one", "item-1")
    first_pass = claim_todo(store, first["id"], card)
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
    second_pass = claim_todo(store, second["id"], card)
    assert second_pass is not None
    assert store.holders()["item-1"]["id"] == second["id"]
    store.stop(second["id"])
    database.close()


def test_agent_stop_closes_running_steps(tmp_path: Path) -> None:
    database = SnapshotDatabase(":memory:")
    store = AgentStore(database)
    checkout = str(tmp_path / "runner")
    agent = waiting_agent(store, "runner", checkout)
    pass_id = claim_todo(store, agent["id"], issue_card(229, "runner", "item-229"))
    assert pass_id is not None
    begin_run_step(store, pass_id, checkout)
    store.stop(agent["id"])
    stopped = store.get(agent["id"])
    assert stopped is not None
    assert stopped["history"][0]["status"] == "stopped"
    assert stopped["history"][0]["steps"][0]["status"] == "stopped"
    database.close()


def test_agent_restart_recovery_interrupts_running_steps(tmp_path: Path) -> None:
    database = SnapshotDatabase(":memory:")
    store = AgentStore(database)
    checkout = str(tmp_path / "restarted")
    second_agent = waiting_agent(store, "restarted-runner", checkout)
    card = issue_card(229, "runner", "item-229")
    second_pass = claim_todo(store, second_agent["id"], card)
    assert second_pass is not None
    begin_run_step(store, second_pass, checkout)
    AgentStore(database).recover_abandoned()
    recovered = store.get(second_agent["id"])
    assert recovered is not None
    assert recovered["status"] == "stopped"
    assert recovered["history"][0]["status"] == "interrupted"
    assert recovered["history"][0]["steps"][0]["status"] == "interrupted"
    database.close()
