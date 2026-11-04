from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from beehaiive.models import ProjectSnapshot
from beehaiive.project import ProjectDataError
from tests.support import SnapshotOnlyProvider

TICKET_SKILL = "---\ndescription: ticket\n---\n"


class EmptyProvider(SnapshotOnlyProvider):
    def fetch_snapshot(self) -> ProjectSnapshot:
        raise ProjectDataError("project metadata unavailable")


def write_skill(root: Path, content: str = TICKET_SKILL) -> Path:
    skill_file = root / "next-ticket" / "SKILL.md"
    skill_file.parent.mkdir(exist_ok=True)
    skill_file.write_text(content, encoding="utf-8")
    return skill_file


def await_http_job(client: TestClient, job_id: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for _ in range(100):
        result = client.get(f"/api/workflows/generate/{job_id}").json()
        if result["status"] != "pending":
            break
        time.sleep(0.01)
    return result


def workflow_state(
    state_id: str, title: str, action: str, **extra: Any
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": state_id,
        "title": title,
        "action": action,
        "max_visits": 1,
    }
    return base | extra


def run_state(
    state_id: str,
    title: str,
    skill: str,
    prompt: str,
    outcomes: list[str],
    **extra: Any,
) -> dict[str, Any]:
    run_fields = {"skill": skill, "prompt": prompt, "outcomes": outcomes}
    return workflow_state(state_id, title, "run_skill", **run_fields, **extra)


def workflow_transition(
    source: str, target: str, condition: dict[str, Any], *, priority: int = 1
) -> dict[str, Any]:
    return {
        "from": source,
        "to": target,
        "priority": priority,
        "conditions": [condition],
    }


def outcome_transition(
    source: str, target: str, outcome: str, *, priority: int = 1
) -> dict[str, Any]:
    condition = {"kind": "outcome_is", "value": outcome}
    return workflow_transition(source, target, condition, priority=priority)


def status_parameter(name: str, value: str) -> dict[str, Any]:
    return {"name": name, "type": "status", "const": True, "value": value}


def delivery_definition(
    *,
    description: str,
    source_prompt: str,
    states: list[dict[str, Any]],
    transitions: list[dict[str, Any]],
    **settings: Any,
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema_version": 1,
        "name": "Delivery",
        "description": description,
        "source_prompt": source_prompt,
        "auto_reset_on_stall": False,
        "max_steps_per_pass": 4,
        "parameters": [],
        "initial": "wait",
        "states": states,
        "transitions": transitions,
    }
    return base | settings


def valid_definition() -> dict[str, Any]:
    wait_to_escalate = workflow_transition(
        "wait", "escalate", {"kind": "item_status_is", "value": "{status}"}
    )
    return delivery_definition(
        description="Wait for work.",
        source_prompt="Wait for backlog.",
        parameters=[status_parameter("status", "Backlog")],
        states=[
            workflow_state("wait", "Wait", "wait_for_work"),
            workflow_state("escalate", "Escalate", "escalate"),
        ],
        transitions=[
            wait_to_escalate,
            workflow_transition("escalate", "wait", {"kind": "always"}),
        ],
    )


def definition(skill: str = "next-ticket") -> dict[str, Any]:
    wait = workflow_state("wait", "Wait", "wait_for_work", max_visits=3)
    prompt = "Work the held item."
    run = run_state("run", "Run", skill, prompt, ["done", "blocked"], max_visits=3)
    escalate = workflow_state("escalate", "Escalate", "escalate", max_visits=3)
    backlog = {"kind": "item_status_is", "value": "{backlog}"}
    transitions = [
        workflow_transition("wait", "run", backlog),
        outcome_transition("run", "wait", "done"),
        outcome_transition("run", "escalate", "blocked", priority=2),
        workflow_transition("escalate", "wait", {"kind": "always"}),
    ]
    return delivery_definition(
        description="Run a skill for a backlog issue.",
        source_prompt="Wait for a backlog item, then run the skill.",
        auto_reset_on_stall=True,
        max_steps_per_pass=20,
        parameters=[status_parameter("backlog", "Backlog")],
        states=[wait, run, escalate],
        transitions=transitions,
    )
