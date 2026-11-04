from __future__ import annotations

import io
import json
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from beehaiive.agents import AgentStore
from beehaiive.held_item import HeldItem
from beehaiive.models import ProjectCard, ProjectColumn, ProjectSnapshot
from tests.workflow_support import (
    delivery_definition,
    outcome_transition,
    run_state,
    status_parameter,
    workflow_state,
    workflow_transition,
)

REPOSITORY = "TychoHenzen/BeeHAIve"
FAKE_RESULT = {
    "outcome": "done",
    "summary": "completed the held item",
    "handover": {"evidence": "fake process"},
}


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
        status_options: Sequence[str] = (),
        status_option_ids: Mapping[str, str] | None = None,
    ) -> HeldItem:
        del status_field_id, status_options, status_option_ids
        for column in self.snapshot.columns:
            for card in column.items:
                if card.item_key == item_key:
                    return HeldItem(card=card, status=column.status)
        raise AssertionError(f"missing fake Project item {item_key}")


class FakeCodexProcess:
    def __init__(self, command: list[str], **_kwargs: Any) -> None:
        self.command = command
        self.returncode = 0
        self.pid = None

    def communicate(self, _prompt: str, timeout: float) -> tuple[str, str]:
        del timeout
        result_path = Path(self.command[self.command.index("-o") + 1])
        result_path.write_text(json.dumps(FAKE_RESULT), encoding="utf-8")
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


def workflow_definition() -> dict[str, Any]:
    wait = workflow_state("wait", "Wait", "wait_for_work")
    run = run_state("run", "Run", "deliver", "Deliver the held item.", ["done"])
    todo = {"kind": "item_status_is", "value": "{status}"}
    return delivery_definition(
        description="Run one skill.",
        source_prompt="Wait for Todo.",
        parameters=[status_parameter("status", "Todo")],
        states=[wait, run],
        transitions=[
            workflow_transition("wait", "run", todo),
            outcome_transition("run", "wait", "done"),
        ],
    )


def issue_card(number: int, title: str, item_key: str | None = None) -> ProjectCard:
    return ProjectCard(
        type="Issue",
        repository=REPOSITORY,
        number=number,
        title=title,
        url=f"https://github.com/{REPOSITORY}/issues/{number}",
        state="open",
        labels=(),
        linked_issue_numbers=(),
        item_key=item_key,
    )


def todo_snapshot() -> ProjectSnapshot:
    card = issue_card(229, "Run assigned workflow", "project-item-229")
    return ProjectSnapshot(
        fetched_at="2026-09-29T10:00:00+00:00",
        rate_limited_until=None,
        columns=(ProjectColumn(status="Todo", items=(card,)),),
    )


def init_checkout(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    origin = f"https://github.com/{REPOSITORY}.git"
    remote_add = ["git", "-C", str(path), "remote", "add", "origin", origin]
    subprocess.run(remote_add, check=True, capture_output=True)
    return path


def waiting_agent(store: AgentStore, name: str, checkout_path: str) -> dict[str, Any]:
    agent = store.create(
        name=name,
        workflow_id=1,
        workflow_revision=1,
        parameters={},
        repository=REPOSITORY,
        checkout_path=checkout_path,
        model=None,
    )
    store.set_status(agent["id"], "waiting")
    return agent


def claim_todo(store: AgentStore, agent_id: str, card: ProjectCard) -> str | None:
    return store.claim(
        agent_id,
        card,
        status="Todo",
        workflow_id=1,
        workflow_revision=1,
        initial_state="wait",
    )


def begin_run_step(store: AgentStore, pass_id: str, checkout_path: str) -> None:
    sequence = store.next_step_sequence(pass_id)
    log_path = store.log_path(checkout_path, pass_id, sequence)
    store.begin_step(pass_id, "run", "run_skill", ["codex", "exec"], str(log_path))
