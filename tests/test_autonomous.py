from __future__ import annotations

import time
from threading import Event

import pytest

from beehaiive.autonomous import (
    ADVISOR_STEP,
    AUTONOMOUS_STEPS,
    AutonomousLifecycleRunner,
    AutonomousLifecycleService,
    PlaceholderSkillExecutor,
    select_work_item,
)
from beehaiive.orchestrator import Orchestrator
from beehaiive.storage import OrchestratorStore
from tests.conftest import FakeProvider
from tests.support.dashboard.helpers import dashboard_snapshot


def test_select_work_item_prefers_todo_over_backlog_and_skips_active() -> None:
    selected = select_work_item(
        [
            {
                "name": "owner/api",
                "active": True,
                "pbis": [
                    {"number": 1, "planning_status": "Backlog", "stage": "backlog"},
                    {"number": 2, "planning_status": "Todo", "stage": "backlog"},
                    {"number": 3, "planning_status": "Todo", "status": "active"},
                ],
            }
        ]
    )

    assert selected == {
        "repository": "owner/api",
        "pbi_number": 2,
        "title": "Untitled PBI",
        "planning_status": "todo",
        "stage": "backlog",
        "subtasks": [],
    }


def test_placeholder_runner_passes_one_branch_handover_through_all_skills() -> None:
    runner = AutonomousLifecycleRunner(PlaceholderSkillExecutor())

    result = runner.run(
        {
            "run_id": "run-1",
            "project_id": "project-1",
            "repository": "owner/api",
            "pbi_number": 2,
            "planning_status": "Backlog",
            "stage": "backlog",
        }
    )

    assert result.status == "completed"
    assert [handoff.step for handoff in result.handoffs] == [
        step.name for step in AUTONOMOUS_STEPS
    ]
    assert all(handoff.handover["single_branch"] for handoff in result.handoffs)


def test_runner_normalizes_completed_skill_status() -> None:
    class CompletedExecutor:
        def execute(self, step, context, handover):
            del step, context
            return {
                "status": "completed",
                "summary": "stage completed",
                "handover": handover,
            }

    steps: list[str] = []
    result = AutonomousLifecycleRunner(CompletedExecutor(), on_step=steps.append).run(
        {
            "project_id": "project-1",
            "repository": "owner/api",
            "pbi_number": 1,
            "planning_status": "Todo",
            "stage": "implement",
        }
    )

    assert result.status == "completed"
    assert len(result.handoffs) == len(AUTONOMOUS_STEPS) - 1
    assert all(handoff.status == "succeeded" for handoff in result.handoffs)
    assert steps == [step.name for step in AUTONOMOUS_STEPS[1:]]


def test_blocker_calls_the_advisor_once() -> None:
    class Blocker:
        def __init__(self) -> None:
            self.calls = 0

        def execute(self, step, context, handover):
            del step, context, handover
            self.calls += 1
            return {"status": "blocked", "summary": "provider unavailable"}

    class Advisor:
        def __init__(self) -> None:
            self.calls = 0

        def execute(self, step, context, handover):
            assert step is ADVISOR_STEP
            assert context["failed_step"] == "refine-backlog-item"
            del handover
            self.calls += 1
            return {"status": "succeeded", "summary": "Use the provider retry path."}

    blocker = Blocker()
    advisor = Advisor()
    result = AutonomousLifecycleRunner(blocker, advisor).run(
        {
            "project_id": "project-1",
            "repository": "owner/api",
            "pbi_number": 1,
            "planning_status": "Backlog",
            "stage": "backlog",
        }
    )

    assert result.status == "blocked"
    assert blocker.calls == 1
    assert advisor.calls == 1
    assert result.advisor is not None and result.advisor.step == "codex-advisor"


def test_autonomous_service_persists_skill_handoffs() -> None:
    store = OrchestratorStore()
    service = Orchestrator(store, FakeProvider(dashboard_snapshot()))
    service.synchronize("project-1")
    automation = AutonomousLifecycleService(
        service, PlaceholderSkillExecutor(), PlaceholderSkillExecutor()
    )

    try:
        started = automation.start("project-1")
        deadline = time.monotonic() + 3
        current = automation.status(str(started["run_id"]))
        while current["status"] == "running" and time.monotonic() < deadline:
            time.sleep(0.01)
            current = automation.status(str(started["run_id"]))
        assert current["status"] == "completed"
        actions = store.actions_for_project("project-1")
        assert any(action["kind"] == "autonomous_start" for action in actions)
        assert {
            action["kind"]
            for action in actions
            if str(action["kind"]).startswith("skill:")
        } == {f"skill:{step.name}" for step in AUTONOMOUS_STEPS}
    finally:
        store.close()


def test_autonomous_service_runs_one_pbi_at_a_time() -> None:
    class BlockingExecutor:
        started = Event()
        release = Event()

        def execute(self, step, context, handover):
            self.started.set()
            self.release.wait(3)
            return PlaceholderSkillExecutor().execute(step, context, handover)

    store = OrchestratorStore()
    service = Orchestrator(store, FakeProvider(dashboard_snapshot()))
    service.synchronize("project-1")
    executor = BlockingExecutor()
    automation = AutonomousLifecycleService(service, executor, executor)

    try:
        started = automation.start("project-1")
        assert executor.started.wait(1)
        assert automation.active_count() == 1
        with pytest.raises(ValueError, match="already running"):
            automation.start("project-1")
        executor.release.set()
        deadline = time.monotonic() + 3
        current = automation.status(str(started["run_id"]))
        while current["status"] == "running" and time.monotonic() < deadline:
            time.sleep(0.01)
            current = automation.status(str(started["run_id"]))
        assert current["status"] == "completed"
        assert automation.active_count() == 0
    finally:
        executor.release.set()
        store.close()


def test_autonomous_service_rejects_a_different_configured_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BEEHAIIVE_AGENT_REPOSITORY_NAME", "owner/configured")
    store = OrchestratorStore()
    service = Orchestrator(store, FakeProvider(dashboard_snapshot()))
    service.synchronize("project-1")
    automation = AutonomousLifecycleService(service, PlaceholderSkillExecutor())

    try:
        with pytest.raises(ValueError, match="outside the configured checkout"):
            automation.start("project-1", repository="owner/api", pbi_number=1)
    finally:
        store.close()
