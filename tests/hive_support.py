from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple

from beehaiive.agents import AgentStore
from beehaiive.database import SnapshotDatabase
from beehaiive.models import ProjectCard, ProjectColumn, ProjectSnapshot
from beehaiive.workflows import WorkflowStore
from tests.agent_support import REPOSITORY, issue_card
from tests.workflow_support import (
    delivery_definition,
    outcome_transition,
    run_state,
    workflow_state,
    workflow_transition,
)

ITEM_KEY = "item-231"
ACTIVE_PASS = "active-pass"
NOW = datetime(2026, 9, 29, 12, tzinfo=UTC)
ACTIVE_STARTED = NOW - timedelta(seconds=70)
FINISHED_SECONDS = (10, 20, 30)
ITEM_JSON = json.dumps(
    {"repository": REPOSITORY, "number": 231, "title": "Hive map"},
)

INSERT_FINISHED_PASS = (
    "INSERT INTO agent_passes(id, agent_id, item_key, item_json, workflow_id, "
    "workflow_revision, current_state, status, started_at, finished_at) "
    "VALUES (?, ?, ?, ?, ?, 1, 'run', 'completed', ?, ?)"
)
INSERT_FINISHED_STEP = (
    "INSERT INTO agent_steps(pass_id, sequence, state_id, action, status, "
    "outcome, summary, started_at, finished_at) VALUES (?, 1, 'run', "
    "'run_skill', 'completed', 'done', 'finished', ?, ?)"
)
INSERT_ACTIVE_PASS = (
    "INSERT INTO agent_passes(id, agent_id, item_key, item_json, workflow_id, "
    "workflow_revision, current_state, status, started_at) VALUES "
    "(?, ?, ?, ?, ?, 1, 'run', 'running', ?)"
)
INSERT_CLAIM = (
    "INSERT INTO agent_claims(item_key, agent_id, pass_id, claimed_at) "
    "VALUES (?, ?, ?, ?)"
)
INSERT_ACTIVE_STEP = (
    "INSERT INTO agent_steps(pass_id, sequence, state_id, action, status, "
    "started_at) VALUES ('active-pass', 1, 'run', 'run_skill', 'running', ?)"
)
MARK_AGENT_WORKING = (
    "UPDATE agents SET status = 'working', current_state = 'run', "
    "current_pass_id = 'active-pass', updated_at = ? WHERE id = ?"
)
INSERT_ALERT = (
    "INSERT INTO agent_alerts(agent_id, pass_id, sequence, kind, message, "
    "created_at) VALUES (?, 'finished-1', 1, 'escalated', "
    "'operator review needed', ?)"
)
CLEAR_INTERRUPTED = (
    "DELETE FROM agent_alerts WHERE agent_id = ? AND kind = 'interrupted'"
)
REOPEN_PASS = (
    "UPDATE agent_passes SET status = 'running', finished_at = NULL, "
    "stalled_reason = NULL WHERE id = 'active-pass'"
)
REOPEN_STEP = (
    "UPDATE agent_steps SET status = 'running', finished_at = NULL, "
    "summary = '' WHERE pass_id = 'active-pass' AND sequence = 1"
)
RECLAIM = (
    "INSERT OR IGNORE INTO agent_claims(item_key, agent_id, pass_id, "
    "claimed_at) SELECT item_key, agent_id, id, ? FROM agent_passes "
    "WHERE id = 'active-pass'"
)


class SeededHive(NamedTuple):
    database: SnapshotDatabase
    agent_id: str
    alert_pass_id: str
    now: datetime


def hive_definition() -> dict[str, Any]:
    depot = workflow_state("wait", "Depot", "wait_for_work", layout={"x": 0, "y": 0})
    prompt = "Deliver the item."
    delivery = run_state(
        "run",
        "Delivery",
        "deliver",
        prompt,
        ["done", "blocked"],
        layout={"x": 280, "y": 0},
    )
    escalation = workflow_state(
        "escalate", "Escalation", "escalate", layout={"x": 560, "y": 0}
    )
    todo = {"kind": "item_status_is", "value": "Todo"}
    transitions = [
        workflow_transition("wait", "run", todo),
        outcome_transition("run", "wait", "done"),
        outcome_transition("run", "escalate", "blocked", priority=2),
        workflow_transition("escalate", "wait", {"kind": "always"}),
    ]
    return delivery_definition(
        description="A small delivery workflow.",
        source_prompt="Wait for Todo, then deliver.",
        max_steps_per_pass=6,
        states=[depot, delivery, escalation],
        transitions=transitions,
    )


def hive_snapshot(card: ProjectCard) -> ProjectSnapshot:
    return ProjectSnapshot(
        fetched_at="2026-09-29T10:00:00+00:00",
        rate_limited_until=None,
        columns=(
            ProjectColumn(status="Todo", items=(card,)),
            ProjectColumn(status="Done", items=()),
        ),
    )


def create_worker(
    database: SnapshotDatabase, workflow_id: int, checkout: Path
) -> dict[str, Any]:
    return AgentStore(database).create(
        name="worker",
        workflow_id=workflow_id,
        workflow_revision=1,
        parameters={},
        repository=REPOSITORY,
        checkout_path=str(checkout),
        model=None,
    )


def insert_finished_pass(
    connection: sqlite3.Connection,
    agent_id: str,
    workflow_id: int,
    index: int,
    seconds: int,
) -> None:
    started_at = NOW - timedelta(minutes=10 + index)
    started = started_at.isoformat()
    finished = (started_at + timedelta(seconds=seconds)).isoformat()
    pass_id = f"finished-{index}"
    item_key = f"old-{index}"
    pass_values = (pass_id, agent_id, item_key, ITEM_JSON, workflow_id)
    connection.execute(INSERT_FINISHED_PASS, (*pass_values, started, finished))
    connection.execute(INSERT_FINISHED_STEP, (pass_id, started, finished))


def insert_active_pass(
    connection: sqlite3.Connection, agent_id: str, workflow_id: int
) -> None:
    started = ACTIVE_STARTED.isoformat()
    pass_values = (ACTIVE_PASS, agent_id, ITEM_KEY, ITEM_JSON, workflow_id, started)
    claim_values = (ITEM_KEY, agent_id, ACTIVE_PASS, NOW.isoformat())
    connection.execute(INSERT_ACTIVE_PASS, pass_values)
    connection.execute(INSERT_CLAIM, claim_values)
    connection.execute(INSERT_ACTIVE_STEP, (started,))


def mark_agent_working(connection: sqlite3.Connection, agent_id: str) -> None:
    connection.execute(MARK_AGENT_WORKING, (NOW.isoformat(), agent_id))


def insert_history(
    connection: sqlite3.Connection, agent_id: str, workflow_id: int
) -> None:
    for index, seconds in enumerate(FINISHED_SECONDS, start=1):
        insert_finished_pass(connection, agent_id, workflow_id, index, seconds)
    insert_active_pass(connection, agent_id, workflow_id)
    mark_agent_working(connection, agent_id)
    connection.execute(INSERT_ALERT, (agent_id, NOW.isoformat()))


def restore_active_pass(connection: sqlite3.Connection, agent_id: str) -> None:
    connection.execute(CLEAR_INTERRUPTED, (agent_id,))
    connection.execute(REOPEN_PASS)
    connection.execute(REOPEN_STEP)
    connection.execute(RECLAIM, (NOW.isoformat(),))
    mark_agent_working(connection, agent_id)


def seed_database(tmp_path: Path) -> SeededHive:
    skill = tmp_path / "deliver" / "SKILL.md"
    skill.parent.mkdir()
    skill.write_text("deliver", encoding="utf-8")
    database = SnapshotDatabase(":memory:")
    workflows = WorkflowStore(database)
    workflow = workflows.create_workflow("Delivery", "prompt", hive_definition())
    workflow_id = int(workflow["id"])
    agent = create_worker(database, workflow_id, tmp_path / "checkout")
    agent_id = str(agent["id"])
    database.save_snapshot(hive_snapshot(issue_card(231, "Hive map", ITEM_KEY)))
    database.transaction(
        lambda connection: insert_history(connection, agent_id, workflow_id)
    )
    return SeededHive(database, agent_id, "finished-1", NOW)
