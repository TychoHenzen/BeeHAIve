from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from beehaiive.agents import AgentStore
from beehaiive.app import create_app
from beehaiive.config import CoreConfig
from beehaiive.database import SnapshotDatabase
from beehaiive.hive import (
    HiveProjection,
    _color,
    _condition_matches,
    _duration,
    _json_object,
    _matches_wait_transition,
    _parameter_value,
    _station_map,
    _timestamp,
    _transition_label,
)
from beehaiive.models import ProjectCard, ProjectColumn, ProjectSnapshot
from beehaiive.workflows import WorkflowStore


def _config(skill_root: Path) -> CoreConfig:
    return CoreConfig(
        owner="TychoHenzen",
        owner_type="user",
        project_number=2,
        github_token="token",
        skills_dirs=(skill_root,),
    )


def _definition() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "name": "Delivery",
        "description": "A small delivery workflow.",
        "source_prompt": "Wait for Todo, then deliver.",
        "auto_reset_on_stall": False,
        "max_steps_per_pass": 6,
        "parameters": [],
        "initial": "wait",
        "states": [
            {
                "id": "wait",
                "title": "Depot",
                "action": "wait_for_work",
                "max_visits": 1,
                "layout": {"x": 0, "y": 0},
            },
            {
                "id": "run",
                "title": "Delivery",
                "action": "run_skill",
                "skill": "deliver",
                "prompt": "Deliver the item.",
                "outcomes": ["done", "blocked"],
                "max_visits": 1,
                "layout": {"x": 280, "y": 0},
            },
            {
                "id": "escalate",
                "title": "Escalation",
                "action": "escalate",
                "max_visits": 1,
                "layout": {"x": 560, "y": 0},
            },
        ],
        "transitions": [
            {
                "from": "wait",
                "to": "run",
                "priority": 1,
                "conditions": [{"kind": "item_status_is", "value": "Todo"}],
            },
            {
                "from": "run",
                "to": "wait",
                "priority": 1,
                "conditions": [{"kind": "outcome_is", "value": "done"}],
            },
            {
                "from": "run",
                "to": "escalate",
                "priority": 2,
                "conditions": [{"kind": "outcome_is", "value": "blocked"}],
            },
            {
                "from": "escalate",
                "to": "wait",
                "priority": 1,
                "conditions": [{"kind": "always"}],
            },
        ],
    }


def _seed_database(tmp_path: Path) -> tuple[SnapshotDatabase, str, str, datetime]:
    skill = tmp_path / "deliver" / "SKILL.md"
    skill.parent.mkdir()
    skill.write_text("deliver", encoding="utf-8")
    database = SnapshotDatabase(":memory:")
    workflows = WorkflowStore(database)
    workflow = workflows.create_workflow("Delivery", "prompt", _definition())
    workflow_id = int(workflow["id"])
    agents = AgentStore(database)
    agent = agents.create(
        name="worker",
        workflow_id=workflow_id,
        workflow_revision=1,
        parameters={},
        repository="TychoHenzen/BeeHAIve",
        checkout_path=str(tmp_path / "checkout"),
        model=None,
    )
    agent_id = str(agent["id"])
    card = ProjectCard(
        type="Issue",
        repository="TychoHenzen/BeeHAIve",
        number=231,
        title="Hive map",
        url="https://github.com/TychoHenzen/BeeHAIve/issues/231",
        state="open",
        labels=(),
        linked_issue_numbers=(),
        item_key="item-231",
    )
    database.save_snapshot(
        ProjectSnapshot(
            fetched_at="2026-09-29T10:00:00+00:00",
            rate_limited_until=None,
            columns=(
                ProjectColumn(status="Todo", items=(card,)),
                ProjectColumn(status="Done", items=()),
            ),
        )
    )
    now = datetime(2026, 9, 29, 12, tzinfo=UTC)
    item_json = json.dumps(
        {"repository": card.repository, "number": card.number, "title": card.title}
    )

    def seed(connection: Any) -> None:
        for index, duration in enumerate((10, 20, 30), start=1):
            started = now - timedelta(minutes=10 + index)
            finished = started + timedelta(seconds=duration)
            pass_id = f"finished-{index}"
            connection.execute(
                "INSERT INTO agent_passes(id, agent_id, item_key, item_json, "
                "workflow_id, "
                "workflow_revision, current_state, status, started_at, finished_at) "
                "VALUES (?, ?, ?, ?, ?, 1, 'run', 'completed', ?, ?)",
                (
                    pass_id,
                    agent_id,
                    f"old-{index}",
                    item_json,
                    workflow_id,
                    started.isoformat(),
                    finished.isoformat(),
                ),
            )
            connection.execute(
                "INSERT INTO agent_steps(pass_id, sequence, state_id, action, status, "
                "outcome, summary, started_at, finished_at) VALUES (?, 1, 'run', "
                "'run_skill', 'completed', 'done', 'finished', ?, ?)",
                (pass_id, started.isoformat(), finished.isoformat()),
            )
        connection.execute(
            "INSERT INTO agent_passes(id, agent_id, item_key, item_json, workflow_id, "
            "workflow_revision, current_state, status, started_at) VALUES "
            "(?, ?, ?, ?, ?, "
            "1, 'run', 'running', ?)",
            (
                "active-pass",
                agent_id,
                card.item_key,
                item_json,
                workflow_id,
                (now - timedelta(seconds=70)).isoformat(),
            ),
        )
        connection.execute(
            "INSERT INTO agent_claims(item_key, agent_id, pass_id, claimed_at) "
            "VALUES (?, ?, ?, ?)",
            (card.item_key, agent_id, "active-pass", now.isoformat()),
        )
        connection.execute(
            "INSERT INTO agent_steps(pass_id, sequence, state_id, action, status, "
            "started_at) "
            "VALUES ('active-pass', 1, 'run', 'run_skill', 'running', ?)",
            ((now - timedelta(seconds=70)).isoformat(),),
        )
        connection.execute(
            "UPDATE agents SET status = 'working', current_state = 'run', "
            "current_pass_id = 'active-pass', updated_at = ? WHERE id = ?",
            (now.isoformat(), agent_id),
        )
        connection.execute(
            "INSERT INTO agent_alerts(agent_id, pass_id, sequence, kind, message, "
            "created_at) "
            "VALUES (?, 'finished-1', 1, 'escalated', 'operator review needed', ?)",
            (agent_id, now.isoformat()),
        )

    database.transaction(seed)
    return database, agent_id, "finished-1", now


def test_hive_projection_exposes_units_layout_timing_waiting_and_alerts(
    tmp_path: Path,
) -> None:
    database, agent_id, alert_pass_id, now = _seed_database(tmp_path)
    projection = HiveProjection(database, clock=lambda: now)

    before = database.transaction(
        lambda connection: (
            connection.execute("SELECT version FROM schema_version").fetchone(),
            connection.execute("SELECT COUNT(*) FROM agent_alerts").fetchone(),
        )
    )
    payload = projection.snapshot()
    after = database.transaction(
        lambda connection: (
            connection.execute("SELECT version FROM schema_version").fetchone(),
            connection.execute("SELECT COUNT(*) FROM agent_alerts").fetchone(),
        )
    )

    assert before == after
    assert payload["waiting"] == {"1": 0}
    region = payload["regions"][0]
    assert region["layout"]["run"] == {"x": 280, "y": 0}
    assert {road["outcome"] for road in region["roads"]} == {
        "always",
        "blocked",
        "done",
    }
    unit = payload["units"][0]
    assert unit["agent_id"] == agent_id
    assert unit["station"] == "run"
    assert unit["item"]["number"] == 231
    assert unit["typical_seconds"] == 20.0
    assert unit["overdue"] is True
    assert payload["alerts"][0]["pass_id"] == alert_pass_id
    assert payload["alerts"][0]["station"] == "run"
    database.close()


def test_hive_helpers_cover_conditions_empty_state_and_idempotent_ack() -> None:
    card = ProjectCard(
        type="PullRequest",
        repository="owner/repo",
        number=4,
        title="Pull",
        url=None,
        state="open",
        labels=("bug",),
        linked_issue_numbers=(),
        item_key="pull-4",
    )
    parameters = {"status": "Todo", "label": "bug"}
    assert _json_object("not-json") == {}
    assert _json_object("[]") == {}
    assert _timestamp(None) is None
    assert _timestamp("not-a-date") is None
    assert _duration(None, "2026-09-29T10:00:00+00:00") is None
    assert _duration("2026-09-29T10:00:00+00:00", "2026-09-29T09:00:00+00:00") == 0
    assert _parameter_value(1, parameters) == 1
    assert _parameter_value("{status}", parameters) == "Todo"
    assert _parameter_value("status={status}/{label}", parameters) == "status=Todo/bug"
    for condition, expected in (
        ({"kind": "always"}, True),
        ({"kind": "item_status_is", "value": "{status}"}, True),
        ({"kind": "item_type_is", "value": "pull_request"}, True),
        ({"kind": "item_has_label", "value": "{label}"}, True),
        ({"kind": "item_lacks_label", "value": "other"}, True),
        ({"kind": "item_repository_is", "value": "owner/repo"}, True),
        ({"kind": "unknown"}, False),
    ):
        assert _condition_matches(condition, card, "Todo", parameters) is expected
    definition = {
        "initial": "wait",
        "transitions": [
            {"from": "other", "priority": 1, "conditions": []},
            {
                "from": "wait",
                "priority": 2,
                "conditions": [{"kind": "item_status_is", "value": "Done"}],
            },
            {
                "from": "wait",
                "priority": 3,
                "conditions": [{"kind": "item_status_is", "value": "{status}"}],
            },
            "bad",
        ],
    }
    assert _matches_wait_transition(definition, card, "Todo", parameters)
    assert not _matches_wait_transition(
        {"initial": "wait", "transitions": [{"from": "wait", "conditions": {}}]},
        card,
        "Todo",
        parameters,
    )
    assert _station_map({"states": [{"id": "wait"}, "bad", {"title": "missing"}]}) == {
        "wait": {"id": "wait"}
    }
    assert (
        _transition_label({"conditions": [{"kind": "outcome_is", "value": "done"}]})
        == "done"
    )
    assert _transition_label({"conditions": ["bad"]}) == "always"
    assert _color("agent")

    database = SnapshotDatabase(":memory:")
    projection = HiveProjection(
        database, clock=lambda: datetime(2026, 9, 29, tzinfo=UTC)
    )
    assert projection.snapshot() == {
        "generated_at": "2026-09-29T00:00:00+00:00",
        "regions": [],
        "units": [],
        "alerts": [],
        "waiting": {},
    }
    assert projection.events("cursor")["events"] == []
    assert projection.events("cursor")["cursor"] == "cursor"
    assert projection.acknowledge("missing") is None
    database.close()


def test_hive_http_events_cursor_and_acknowledgement(tmp_path: Path) -> None:
    database, agent_id, alert_pass_id, now = _seed_database(tmp_path)
    config = _config(tmp_path)
    app = create_app(config, database=database)
    database.transaction(
        lambda connection: (
            connection.execute(
                "DELETE FROM agent_alerts WHERE agent_id = ? AND kind = 'interrupted'",
                (agent_id,),
            ),
            connection.execute(
                "UPDATE agent_passes SET status = 'running', finished_at = NULL, "
                "stalled_reason = NULL WHERE id = 'active-pass'"
            ),
            connection.execute(
                "UPDATE agent_steps SET status = 'running', finished_at = NULL, "
                "summary = '' WHERE pass_id = 'active-pass' AND sequence = 1"
            ),
            connection.execute(
                "INSERT OR IGNORE INTO agent_claims(item_key, agent_id, pass_id, "
                "claimed_at) SELECT item_key, agent_id, id, ? FROM agent_passes "
                "WHERE id = 'active-pass'",
                (now.isoformat(),),
            ),
            connection.execute(
                "UPDATE agents SET status = 'working', current_state = 'run', "
                "current_pass_id = 'active-pass', updated_at = ? WHERE id = ?",
                (now.isoformat(), agent_id),
            ),
        )
    )
    with TestClient(app) as client:
        hive = client.get("/api/hive")
        events = client.get("/api/hive/events?since=0")
        cursor = events.json()["cursor"]
        replay = client.get("/api/hive/events", params={"since": cursor})
        acknowledged = client.post(
            f"/api/passes/{alert_pass_id}/acknowledge",
            json={"acknowledged_by": "operator@example.test"},
        )
        after_ack = client.get("/api/hive")
        page = client.get("/")
        script = client.get("/web/hive.js")
        history = client.get(
            f"/api/agents/{hive.json()['units'][0]['agent_id']}/history"
        )

    assert hive.status_code == 200
    assert events.status_code == 200
    assert events.json()["events"]
    assert replay.json()["events"] == []
    assert acknowledged.status_code == 200
    assert acknowledged.json()["acknowledged_by"] == "operator@example.test"
    assert after_ack.json()["alerts"] == []
    assert page.status_code == 200
    assert 'id="hive-map"' in page.text
    assert script.status_code == 200
    assert "/api/hive/events" in script.text
    acknowledged_pass = next(
        value for value in history.json()["passes"] if value["id"] == alert_pass_id
    )
    assert acknowledged_pass["acknowledged_at"]
    assert acknowledged_pass["acknowledged_by"] == "operator@example.test"
    database.close()
