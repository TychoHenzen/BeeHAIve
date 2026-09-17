from __future__ import annotations

import json
import time
from pathlib import Path
from threading import Event

import pytest

import beehaiive.autonomous as autonomous
from beehaiive.autonomous import (
    ADVISOR_STEP,
    AUTONOMOUS_STEPS,
    IDEA_CAPTURE_STEP,
    AutonomousLifecycleRunner,
    AutonomousLifecycleService,
    PlaceholderSkillExecutor,
    select_work_item,
)
from beehaiive.models import PbiSnapshot, ProjectSnapshot, RepositorySnapshot, Stage
from beehaiive.orchestrator import Orchestrator
from beehaiive.storage import OrchestratorStore
from tests.conftest import FakeProvider
from tests.support.agent.helpers import make_git_repository, workflow_service_for
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


def test_autonomous_explicit_resume_accepts_an_in_progress_pbi() -> None:
    store = OrchestratorStore()
    service = Orchestrator(store, FakeProvider(dashboard_snapshot()))
    automation = AutonomousLifecycleService(service, PlaceholderSkillExecutor())

    try:
        selected = automation._select(
            {
                "repositories": [
                    {
                        "name": "owner/api",
                        "active": True,
                        "pbis": [
                            {
                                "number": 1,
                                "planning_status": "In Progress",
                                "stage": "implement",
                            }
                        ],
                    }
                ]
            },
            "owner/api",
            1,
            set(),
        )
        assert selected is not None and selected["pbi_number"] == 1
    finally:
        store.close()


def test_autonomous_selection_resumes_after_a_published_pull_request() -> None:
    store = OrchestratorStore()
    service = Orchestrator(store, FakeProvider(dashboard_snapshot()))
    automation = AutonomousLifecycleService(service, PlaceholderSkillExecutor())
    actions = [
        {
            "kind": "skill:submit-draft-pr",
            "status": "succeeded",
            "repository": "owner/api",
            "pbi_number": 1,
            "result": {
                "status": "succeeded",
                "handover": {
                    "branch": "codex/resume",
                    "pull_request": 9,
                    "head": "head-9",
                },
            },
        },
        {
            "kind": "autonomous_start",
            "status": "failed",
            "repository": "owner/api",
            "pbi_number": 1,
        },
    ]

    try:
        resumable = automation._resumable_items(actions)
        selected = automation._select(
            {
                "repositories": [
                    {
                        "name": "owner/api",
                        "active": True,
                        "pbis": [
                            {
                                "number": 1,
                                "planning_status": "In Progress",
                                "stage": "implement",
                            }
                        ],
                    }
                ]
            },
            None,
            None,
            set(),
            resumable,
        )
        assert selected is not None
        resume = automation._resume_context(actions, "owner/api", 1)
        assert resume["resume_step"] == "review-pr-branch"
        assert autonomous._start_index({**selected, **resume}) == 3
    finally:
        store.close()


def test_autonomous_resume_retries_a_draft_publication() -> None:
    actions = [
        {
            "kind": "skill:submit-draft-pr",
            "status": "succeeded",
            "repository": "owner/api",
            "pbi_number": 1,
            "result": {
                "status": "succeeded",
                "handover": {
                    "branch": "codex/draft",
                    "pull_request": 9,
                    "head": "head-9",
                    "draft": True,
                },
            },
        }
    ]

    assert AutonomousLifecycleService._resume_context(actions, "owner/api", 1) == {
        "resume_step": "submit-draft-pr",
        "resume_existing_workspace": True,
        "branch": "codex/draft",
        "workspace_branch": "codex/draft",
        "pull_request": 9,
        "head": "head-9",
    }


def test_autonomous_start_resumes_the_next_lifecycle_skill() -> None:
    snapshot = ProjectSnapshot(
        "project-1",
        "Planning",
        (
            RepositorySnapshot(
                "owner/api",
                (
                    PbiSnapshot(
                        "owner/api",
                        5,
                        "Resume me",
                        Stage.IMPLEMENT,
                        "In Progress",
                    ),
                ),
            ),
        ),
    )
    store = OrchestratorStore()
    service = Orchestrator(store, FakeProvider(snapshot))
    service.synchronize("project-1")
    skill_action = store.begin_action(
        "project-1",
        "skill:submit-draft-pr",
        {
            "status": "succeeded",
            "handover": {
                "branch": "codex/resume",
                "pull_request": 9,
                "head": "head-9",
            },
        },
        "owner/api",
        5,
        "old-run",
    )
    store.finish_action(
        str(skill_action["id"]),
        "succeeded",
        {
            "status": "succeeded",
            "handover": {
                "branch": "codex/resume",
                "pull_request": 9,
                "head": "head-9",
            },
        },
    )
    store.begin_action(
        "project-1",
        "autonomous_start",
        {"repository": "owner/api", "pbi_number": 5},
        "owner/api",
        5,
        "old-run",
    )
    actions = store.actions_for_project("project-1")
    failed_start = next(item for item in actions if item["kind"] == "autonomous_start")
    store.finish_action(str(failed_start["id"]), "failed", error="interrupted")
    automation = AutonomousLifecycleService(service, PlaceholderSkillExecutor())

    try:
        started = automation.start("project-1")
        deadline = time.monotonic() + 3
        current = automation.status(str(started["run_id"]))
        while current["status"] == "running" and time.monotonic() < deadline:
            time.sleep(0.01)
            current = automation.status(str(started["run_id"]))
        assert current["status"] == "completed"
        new_steps = {
            action["kind"]
            for action in store.actions_for_project("project-1")
            if action.get("run_id") == started["run_id"]
        }
        assert new_steps == {
            "autonomous_start",
            "skill:review-pr-branch",
            "skill:fix-pr-review",
            "skill:complete-pr",
        }
    finally:
        store.close()


def test_autonomous_backlog_always_starts_with_refinement() -> None:
    assert (
        autonomous._start_index({"planning_status": "Backlog", "stage": "refine"}) == 0
    )
    assert (
        autonomous._start_index({"planning_status": "Backlog", "stage": "implement"})
        == 0
    )
    assert autonomous._start_index({"planning_status": "Todo", "stage": "backlog"}) == 1


def test_autonomous_resume_starts_after_a_published_pull_request() -> None:
    actions = [
        {
            "kind": "skill:next-ticket",
            "status": "failed",
            "repository": "owner/api",
            "pbi_number": 1,
            "result": {"status": "blocked", "handover": {"pbi_number": 1}},
        },
        {
            "kind": "skill:submit-draft-pr",
            "status": "failed",
            "repository": "owner/api",
            "pbi_number": 1,
            "result": {
                "status": "published",
                "handover": {
                    "branch": "codex/fixture",
                    "pull_request": 9,
                    "draft": False,
                    "head": "a" * 40,
                },
            },
        }
    ]

    resume = AutonomousLifecycleService._resume_context(actions, "owner/api", 1)

    assert resume == {
        "resume_step": "review-pr-branch",
        "resume_existing_workspace": True,
        "branch": "codex/fixture",
        "workspace_branch": "codex/fixture",
        "pull_request": 9,
        "head": "a" * 40,
    }


def test_autonomous_resume_fixes_findings_after_a_completed_review() -> None:
    actions = [
        {
            "kind": "skill:review-pr-branch",
            "status": "failed",
            "repository": "owner/api",
            "pbi_number": 1,
            "result": {
                "status": "blocked",
                "summary": "review-pr-branch completed",
                "handover": {"review_complete": True},
                "session_output": (
                    "[stdout]\n"
                    + json.dumps(
                        {
                            "status": "blocked",
                            "summary": {
                                "branch": "codex/fixture",
                                "pull_request": 9,
                                "head_commit": "b" * 40,
                                "findings": [{"severity": "MAJOR"}],
                            },
                            "handover": {"review_complete": True},
                        }
                    )
                    + "\n\n[stderr]\n"
                ),
            },
        },
        {
            "kind": "skill:submit-draft-pr",
            "status": "failed",
            "repository": "owner/api",
            "pbi_number": 1,
            "result": {
                "status": "published",
                "handover": {
                    "branch": "codex/fixture",
                    "pull_request": 9,
                    "head": "b" * 40,
                },
            },
        },
        {
            "kind": "skill:submit-draft-pr",
            "status": "failed",
            "repository": "owner/api",
            "pbi_number": 1,
            "result": {
                "status": "published",
                "handover": {
                    "branch": "codex/older",
                    "pr": "https://github.com/owner/api/pull/8",
                    "head": "c" * 40,
                },
            },
        },
    ]

    resume = AutonomousLifecycleService._resume_context(actions, "owner/api", 1)

    assert resume == {
        "resume_step": "fix-pr-review",
        "resume_existing_workspace": True,
        "branch": "codex/fixture",
        "workspace_branch": "codex/fixture",
        "pull_request": 9,
        "head_commit": "b" * 40,
        "review_findings": [{"severity": "MAJOR"}],
    }


def test_autonomous_resume_retries_a_blocked_completion_stage() -> None:
    actions = [
        {
            "kind": "skill:complete-pr",
            "status": "failed",
            "repository": "owner/api",
            "pbi_number": 1,
            "result": {
                "status": "blocked",
                "handover": {
                    "branch": "codex/fixture",
                    "pull_request": 9,
                    "head_commit": "b" * 40,
                },
            },
        }
    ]

    resume = AutonomousLifecycleService._resume_context(actions, "owner/api", 1)

    assert resume == {
        "resume_step": "complete-pr",
        "resume_existing_workspace": True,
        "branch": "codex/fixture",
        "workspace_branch": "codex/fixture",
        "pull_request": 9,
        "head_commit": "b" * 40,
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


def test_runner_normalizes_published_skill_status() -> None:
    assert autonomous._skill_status("published") == "succeeded"
    assert autonomous._skill_status("reviewed") == "succeeded"
    assert autonomous._skill_status("fixed") == "succeeded"
    assert autonomous._skill_status("merged") == "succeeded"


def test_runner_keeps_bounded_skill_session_output() -> None:
    class SessionExecutor:
        def execute(self, step, _context, handover):
            return {
                "status": "succeeded",
                "summary": "stage completed",
                "session_output": f"session output for {step.name}",
                "handover": handover,
            }

    result = AutonomousLifecycleRunner(SessionExecutor()).run(
        {
            "project_id": "project-1",
            "repository": "owner/api",
            "pbi_number": 1,
            "planning_status": "Todo",
            "stage": "implement",
        }
    )

    assert result.handoffs[0].session_output == ("session output for next-ticket")


def test_runner_passes_the_previous_handover_into_the_next_context() -> None:
    contexts: dict[str, dict[str, object]] = {}

    class HandoverExecutor:
        def execute(self, step, context, handover):
            contexts[step.name] = dict(context)
            return {
                "status": "succeeded",
                "summary": "stage completed",
                "handover": {
                    **handover,
                    "project_status": "Todo",
                    "next_stage": "implementation",
                },
            }

    result = AutonomousLifecycleRunner(HandoverExecutor()).run(
        {
            "project_id": "project-1",
            "repository": "owner/api",
            "pbi_number": 1,
            "planning_status": "Backlog",
            "stage": "backlog",
        }
    )

    assert result.status == "completed"
    assert contexts["next-ticket"]["planning_status"] == "Todo"
    assert contexts["next-ticket"]["stage"] == "implement"


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


def test_autonomous_service_redacts_persisted_model_output() -> None:
    class LeakyExecutor:
        def execute(self, _step, _context, handover):
            return {
                "status": "succeeded",
                "summary": "stage completed",
                "handover": {
                    **handover,
                    "api_token": "secret-token",
                    "branch": "codex/safe",
                },
                "session_output": "raw model output",
            }

    store = OrchestratorStore()
    service = Orchestrator(store, FakeProvider(dashboard_snapshot()))
    service.synchronize("project-1")
    automation = AutonomousLifecycleService(service, LeakyExecutor())

    try:
        started = automation.start("project-1")
        deadline = time.monotonic() + 3
        current = automation.status(str(started["run_id"]))
        while current["status"] == "running" and time.monotonic() < deadline:
            time.sleep(0.01)
            current = automation.status(str(started["run_id"]))
        assert current["status"] == "completed"
        persisted = json.dumps(store.actions_for_project("project-1"))
        assert "raw model output" not in persisted
        assert "secret-token" not in persisted
    finally:
        store.close()


def test_autonomous_service_captures_one_idea_without_persisting_session_output() -> (
    None
):
    class CaptureExecutor:
        def __init__(self) -> None:
            self.calls = 0

        def execute(self, step, context, handover):
            assert step is IDEA_CAPTURE_STEP
            assert context["project_id"] == "project-1"
            assert context["idea"] == "Add a queue"
            assert handover["single_step"] is True
            self.calls += 1
            return {
                "status": "succeeded",
                "summary": "Created Backlog issue #8.",
                "handover": {"issue_number": 8, "project_status": "Backlog"},
                "session_output": "raw model output should not be stored",
            }

    store = OrchestratorStore()
    service = Orchestrator(
        store,
        FakeProvider(
            dashboard_snapshot(
                extra_pbis=(
                    PbiSnapshot("owner/api", 8, "Captured", planning_status="Backlog"),
                )
            )
        ),
    )
    executor = CaptureExecutor()
    automation = AutonomousLifecycleService(service, executor)
    action = store.begin_action("project-1", "capture_idea", {"idea": "Add a queue"})

    try:
        started = automation.capture_idea("project-1", "Add a queue", action["id"])
        deadline = time.monotonic() + 3
        current = automation.status(str(started["run_id"]))
        while current["status"] == "running" and time.monotonic() < deadline:
            time.sleep(0.01)
            current = automation.status(str(started["run_id"]))
        assert current["status"] == "completed"
        assert executor.calls == 1
        recorded = next(
            item
            for item in store.actions_for_project("project-1")
            if item["id"] == action["id"]
        )
        assert recorded["status"] == "succeeded"
        assert recorded["result"] == {
            "status": "succeeded",
            "summary": "Created Backlog issue #8.",
            "handover": {"issue_number": 8, "project_status": "Backlog"},
        }
    finally:
        store.close()


def test_idea_capture_isolates_codex_workspace_from_server_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    protected = tmp_path / "server-checkout"
    protected.mkdir()
    sentinel = protected / "sentinel.txt"
    sentinel.write_text("unchanged\n", encoding="utf-8")
    created: list[Path] = []

    class WritingExecutor:
        def __init__(self, repository, *_args, **_kwargs) -> None:
            self.repository = Path(repository)
            created.append(self.repository)

        def execute(self, _step, _context, _handover):
            (self.repository / "intrusion.txt").write_text(
                "temporary only\n", encoding="utf-8"
            )
            return {"status": "blocked", "summary": "fixture stopped"}

    monkeypatch.setenv("BEEHAIIVE_AGENT_REPOSITORY_NAME", "owner/api")
    monkeypatch.setenv("BEEHAIIVE_AUTONOMOUS_MODE", "codex")
    monkeypatch.setattr(autonomous, "CodexSkillExecutor", WritingExecutor)
    store = OrchestratorStore()
    service = Orchestrator(store, FakeProvider(dashboard_snapshot()))
    automation = AutonomousLifecycleService(service)
    action = store.begin_action("project-1", "capture_idea", {"idea": "isolated"})

    try:
        started = automation.capture_idea("project-1", "isolated", action["id"])
        deadline = time.monotonic() + 3
        current = automation.status(str(started["run_id"]))
        while current["status"] == "running" and time.monotonic() < deadline:
            time.sleep(0.01)
            current = automation.status(str(started["run_id"]))
        assert current["status"] == "blocked"
        assert created and all(path != protected for path in created)
        while any(path.exists() for path in created) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert all(not path.exists() for path in created)
        assert sentinel.read_text(encoding="utf-8") == "unchanged\n"
    finally:
        store.close()


def test_idea_capture_rejects_success_without_backlog_evidence() -> None:
    class InvalidExecutor:
        def execute(self, _step, _context, _handover):
            return {"status": "succeeded", "summary": "not enough evidence"}

    store = OrchestratorStore()
    service = Orchestrator(store, FakeProvider(dashboard_snapshot()))
    automation = AutonomousLifecycleService(service, InvalidExecutor())
    action = store.begin_action("project-1", "capture_idea", {"idea": "unverified"})

    try:
        started = automation.capture_idea("project-1", "unverified", action["id"])
        deadline = time.monotonic() + 3
        current = automation.status(str(started["run_id"]))
        while current["status"] == "running" and time.monotonic() < deadline:
            time.sleep(0.01)
            current = automation.status(str(started["run_id"]))
        assert current["status"] == "blocked"
        recorded = next(
            item
            for item in store.actions_for_project("project-1")
            if item["id"] == action["id"]
        )
        assert recorded["status"] == "failed"
        assert "no verified Backlog issue" in recorded["error"]
    finally:
        store.close()


def test_autonomous_service_marks_orphaned_pending_runs_after_restart() -> None:
    store = OrchestratorStore()
    service = Orchestrator(store, FakeProvider(dashboard_snapshot()))
    service.synchronize("project-1")
    action = store.begin_action(
        "project-1",
        "autonomous_start",
        {"repository": "owner/api", "pbi_number": 1},
        "owner/api",
        1,
        "orphaned-run",
    )
    automation = AutonomousLifecycleService(service, PlaceholderSkillExecutor())

    try:
        assert automation.recover_pending(("project-1",)) == 1
        recovered = next(
            item
            for item in store.actions_for_project("project-1")
            if item["id"] == action["id"]
        )
        assert recovered["status"] == "failed"
        assert "did not survive the server restart" in recovered["error"]
    finally:
        store.close()


def test_autonomous_service_marks_orphaned_idea_capture_after_restart() -> None:
    store = OrchestratorStore()
    service = Orchestrator(store, FakeProvider(dashboard_snapshot()))
    action = store.begin_action(
        "project-1",
        "capture_idea",
        {"idea": "orphaned"},
        run_id="orphaned-idea",
    )
    automation = AutonomousLifecycleService(service, PlaceholderSkillExecutor())

    try:
        assert automation.recover_pending(("project-1",)) == 1
        recovered = next(
            item
            for item in store.actions_for_project("project-1")
            if item["id"] == action["id"]
        )
        assert recovered["status"] == "failed"
        assert "Idea capture did not survive" in recovered["error"]
    finally:
        store.close()


def test_codex_autonomous_run_uses_a_server_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = make_git_repository(tmp_path / "repository")
    workflow_service, workflow_store = workflow_service_for(tmp_path, repository)
    store = OrchestratorStore()
    orchestrator = Orchestrator(store, FakeProvider(dashboard_snapshot()))
    orchestrator.synchronize("project-1")
    workspaces: list[Path] = []

    class RecordingExecutor:
        def __init__(self, path, *_args, **_kwargs) -> None:
            self.repository = Path(path)

        def execute(self, _step, context, handover):
            workspaces.append(self.repository)
            assert context["workspace_path"] == str(self.repository)
            assert context["workspace_branch"].startswith("codex/beehaiive-autonomous-")
            return {
                "status": "succeeded",
                "summary": "stage completed",
                "handover": handover,
            }

    monkeypatch.setenv("BEEHAIIVE_AUTONOMOUS_MODE", "codex")
    monkeypatch.setattr(autonomous, "CodexSkillExecutor", RecordingExecutor)
    service = AutonomousLifecycleService(
        orchestrator,
        workflow_service=workflow_service,
    )

    try:
        started = service.start("project-1", "owner/api", 1)
        deadline = time.monotonic() + 3
        current = service.status(str(started["run_id"]))
        while current["status"] == "running" and time.monotonic() < deadline:
            time.sleep(0.01)
            current = service.status(str(started["run_id"]))
        assert current["status"] == "completed"
        assert workspaces
        assert all(path != repository for path in workspaces)
        assert len({str(path) for path in workspaces}) == 1
        workspace_root = (
            repository.parent / ".repository.beehaiive" / "autonomous-worktrees"
        )
        assert not any(workspace_root.glob("*")), current.get("workspace_cleanup_error")
    finally:
        workflow_store.close()
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
