from __future__ import annotations

# ruff: noqa: E402
import json
import sqlite3
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from beehaiive.autonomous import (
    AUTONOMOUS_STEPS,
    AutonomousLifecycleService,
    SkillStep,
)
from beehaiive.models import PbiSnapshot, ProjectSnapshot, RepositorySnapshot
from beehaiive.orchestrator import Orchestrator
from beehaiive.scheduler import AgentScheduler, SchedulerConfig
from beehaiive.storage import OrchestratorStore

_PROJECTS = ("project-1", "project-2")


class _LifecycleFixtureExecutor:
    def execute(
        self,
        step: SkillStep,
        context: Mapping[str, object],
        handover: Mapping[str, object],
    ) -> Mapping[str, object]:
        project_id = str(context["project_id"])
        branch = f"codex/automation-proof-{project_id}"
        pull_request = 100 + _PROJECTS.index(project_id)
        remaining = AUTONOMOUS_STEPS[AUTONOMOUS_STEPS.index(step) + 1 :]
        next_step = remaining[0].name if remaining else "complete"
        complete = step.name == "complete-pr"
        child_statuses = {
            "implementation": "Done" if complete else "In Progress",
            "wiring": "Done" if complete else "In Progress",
            "quality": "Done" if complete else "In Progress",
            "reliability": "Done" if complete else "In Progress",
        }
        return {
            "status": "succeeded",
            "summary": f"Disposable lifecycle completed {step.name}.",
            "handover": {
                **dict(handover),
                "project_id": project_id,
                "branch": branch,
                "workspace_branch": branch,
                "pull_request": pull_request,
                "pr": pull_request,
                "single_branch": True,
                "subtasks_same_branch": True,
                "reviewAttempted": bool(
                    handover.get("reviewAttempted") or step.name == "review-pr-branch"
                ),
                "checks": {"required": True, "passed": True},
                "child_statuses": child_statuses,
                "parent_status": "Done" if complete else "In Progress",
                "project_status": "Done" if complete else "In Progress",
                "completed_skill": step.name,
                "next_skill": next_step,
            },
        }


class _FixtureProvider:
    def __init__(self, project_ids: Sequence[str]) -> None:
        self.snapshots = {
            project_id: ProjectSnapshot(
                project_id,
                f"Proof {project_id}",
                (
                    RepositorySnapshot(
                        f"owner/{project_id}",
                        (PbiSnapshot(f"owner/{project_id}", 1, "Proof PBI"),),
                    ),
                ),
            )
            for project_id in project_ids
        }

    def discover_project(self, project_id: str) -> ProjectSnapshot:
        return self.snapshots[project_id]


class _FixtureWorker:
    def __init__(self) -> None:
        self.workflow_service = object()
        self.executor = SimpleNamespace(task="proof")
        self.maximum = 0
        self.active_worker_count = 0

    def set_max_concurrent_workers(self, maximum: int) -> None:
        self.maximum = maximum

    def has_capacity(self) -> bool:
        return self.active_worker_count < self.maximum

    def recover(self, _project_ids: Sequence[str]) -> tuple[str, ...]:
        return ()


def _wait_for_runs(service: AutonomousLifecycleService, run_ids: Sequence[str]) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if all(service.status(run_id).get("status") != "running" for run_id in run_ids):
            return
        time.sleep(0.01)
    raise RuntimeError("Disposable lifecycle fixture did not finish")


def _real_fixture_run(
    project_ids: Sequence[str], *, parallel: bool
) -> tuple[
    dict[str, dict[str, object]], dict[str, list[dict[str, object]]], dict[str, object]
]:
    with tempfile.TemporaryDirectory(prefix="beehaiive-automation-proof-") as directory:
        store = OrchestratorStore(Path(directory) / "state.db")
        provider = _FixtureProvider(project_ids)
        orchestrator = Orchestrator(store, cast(Any, provider))
        service = AutonomousLifecycleService(
            orchestrator, _LifecycleFixtureExecutor(), max_concurrency=2
        )
        run_ids: dict[str, str] = {}
        scheduler_started: tuple[str, ...] = ()
        peak_active = 0
        try:
            if parallel:
                worker = cast(Any, _FixtureWorker())
                scheduler = AgentScheduler(
                    orchestrator,
                    worker,
                    set(project_ids),
                    SchedulerConfig(enabled=True, max_concurrency=2),
                    autonomous_start=service.start,
                    autonomous_has_capacity=service.has_capacity,
                    autonomous_active_count=service.active_count,
                )
                scheduler_started = scheduler.poll_once()
                for project_id, run_id in zip(
                    project_ids, scheduler_started, strict=True
                ):
                    run_ids[project_id] = run_id
                peak_active = service.active_count()
            else:
                for project_id in project_ids:
                    orchestrator.synchronize(project_id)
                    started = service.start(
                        project_id, repository=f"owner/{project_id}", pbi_number=1
                    )
                    run_ids[project_id] = str(started["run_id"])
                    peak_active = max(peak_active, service.active_count())
                    _wait_for_runs(service, (run_ids[project_id],))
            _wait_for_runs(service, tuple(run_ids.values()))
            results: dict[str, dict[str, object]] = {}
            persisted: dict[str, list[dict[str, object]]] = {}
            for project_id, run_id in run_ids.items():
                actions = store.actions_for_project(project_id)
                handoffs: list[dict[str, object]] = []
                for action in actions:
                    if not str(action.get("kind", "")).startswith("skill:"):
                        continue
                    result = dict(cast(Mapping[str, object], action.get("result", {})))
                    result["step"] = str(action["kind"])[6:]
                    handoffs.append(result)
                results[project_id] = {
                    "status": service.status(run_id).get("status"),
                    "handoffs": handoffs,
                }
                persisted[project_id] = handoffs
            readback: dict[str, list[dict[str, object]]] = {
                project_id: [] for project_id in project_ids
            }
            connection = sqlite3.connect(Path(directory) / "state.db")
            try:
                rows = connection.execute(
                    "SELECT project_id, kind, result_json FROM actions "
                    "WHERE kind LIKE 'skill:%' ORDER BY created_at"
                ).fetchall()
            finally:
                connection.close()
            for project_id, kind, result_json in rows:
                result = cast(dict[str, object], json.loads(result_json or "{}"))
                result["step"] = str(kind)[6:]
                readback.setdefault(str(project_id), []).append(result)
            return (
                results,
                readback,
                {
                    "scheduler_used": parallel,
                    "started_count": len(scheduler_started),
                    "peak_active_count": peak_active,
                    "max_concurrency": 2,
                },
            )
        finally:
            store.close()


def _mappings(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    sequence = cast(Sequence[object], value)
    return [
        cast(Mapping[str, object], item)
        for item in sequence
        if isinstance(item, Mapping)
    ]


def _handover(handoff: Mapping[str, object]) -> Mapping[str, object]:
    return cast(Mapping[str, object], handoff.get("handover", {}))


def _handoff_single_branch(handoff: Mapping[str, object]) -> bool:
    return _handover(handoff).get("single_branch") is True


def _handoff_has_quality_evidence(handoff: Mapping[str, object]) -> bool:
    handover = _handover(handoff)
    checks = cast(Mapping[str, object], handover.get("checks", {}))
    child_statuses = cast(Mapping[str, object], handover.get("child_statuses", {}))
    return (
        isinstance(handover.get("completed_skill"), str)
        and isinstance(handover.get("next_skill"), str)
        and checks.get("passed") is True
        and len(child_statuses) == 4
    )


def _evidence(
    results: Mapping[str, Mapping[str, object]],
    persisted: Mapping[str, Sequence[Mapping[str, object]]],
) -> dict[str, object]:
    handoffs = [
        handoff
        for result in results.values()
        for handoff in _mappings(result.get("handoffs"))
    ]
    serialized = json.dumps(results, ensure_ascii=False, separators=(",", ":"))
    handoffs_present = bool(handoffs)
    persisted_handoffs = [
        handoff
        for project_handoffs in persisted.values()
        for handoff in project_handoffs
    ]
    branches_by_project = {
        project: {str(_handover(handoff).get("branch")) for handoff in project_handoffs}
        for project, project_handoffs in persisted.items()
    }
    reviews_by_project = {
        project: sum(
            handoff.get("step") == "review-pr-branch" for handoff in project_handoffs
        )
        for project, project_handoffs in persisted.items()
    }
    complete_handoffs = [
        handoff
        for handoff in handoffs
        if _handover(handoff).get("completed_skill") == "complete-pr"
    ]
    reconciled = bool(complete_handoffs) and all(
        _handover(handoff).get("parent_status") == "Done"
        and _handover(handoff).get("project_status") == "Done"
        and len(
            cast(Mapping[str, object], _handover(handoff).get("child_statuses", {}))
        )
        == 4
        for handoff in complete_handoffs
    )
    return {
        "projects": len(results),
        "completed_projects": sum(
            result.get("status") == "completed" for result in results.values()
        ),
        "handoff_count": len(handoffs),
        "handoffs_present": handoffs_present,
        "handoffs_persisted": len(persisted_handoffs) == len(handoffs)
        and set(persisted) == set(results),
        "all_handoffs_single_branch": all(
            _handoff_single_branch(handoff) for handoff in handoffs
        ),
        "single_branch_per_project": all(
            len(branches) == 1 and "None" not in branches
            for branches in branches_by_project.values()
        ),
        "at_most_one_review_per_project": all(
            count <= 1 for count in reviews_by_project.values()
        ),
        "all_handoffs_succeeded": all(
            handoff.get("status") == "succeeded" for handoff in handoffs
        ),
        "quality_evidence": handoffs_present
        and all(_handoff_has_quality_evidence(handoff) for handoff in handoffs)
        and reconciled,
        "bounded_payload": len(serialized.encode("utf-8")) <= 64_000,
        "raw_session_output": any(
            bool(handoff.get("session_output")) for handoff in handoffs
        ),
    }


def _restart_recovery_evidence() -> dict[str, bool]:
    with tempfile.TemporaryDirectory(prefix="beehaiive-restart-proof-") as directory:
        database = Path(directory) / "state.db"
        first_store = OrchestratorStore(database)
        first_orchestrator = Orchestrator(
            first_store, cast(Any, _FixtureProvider(("project-1",)))
        )
        first_orchestrator.synchronize("project-1")
        outer = first_store.begin_action(
            "project-1",
            "autonomous_start",
            {"repository": "owner/project-1", "pbi_number": 1},
            "owner/project-1",
            1,
            "orphaned-run",
        )
        first_store.finish_action(
            str(outer["id"]),
            "failed",
            {"resume_after_restart": True},
            "worker process lost",
        )
        review = first_store.begin_action(
            "project-1",
            "skill:review-pr-branch",
            {
                "reviewAttempted": True,
                "branch": "codex/automation-proof-project-1",
            },
            "owner/project-1",
            1,
            "orphaned-run",
        )
        first_store.finish_action(
            str(review["id"]),
            "failed",
            {"reviewAttempted": True},
            "review process interrupted",
        )
        first_store.close()

        store = OrchestratorStore(database)
        orchestrator = Orchestrator(store, cast(Any, _FixtureProvider(("project-1",))))
        service = AutonomousLifecycleService(
            orchestrator, _LifecycleFixtureExecutor(), max_concurrency=1
        )
        try:
            actions = store.actions_for_project("project-1")
            resume = AutonomousLifecycleService.resume_context_for_recovery(
                actions, "owner/project-1", 1
            )
            started = service.start(
                "project-1", repository="owner/project-1", pbi_number=1
            )
            _wait_for_runs(service, (str(started["run_id"]),))
            review_count = sum(
                action.get("kind") == "skill:review-pr-branch"
                for action in store.actions_for_project("project-1")
            )
            return {
                "resumes_from_checkpoint": resume.get("resume_step") == "fix-pr-review",
                "preserves_branch": resume.get("branch")
                == "codex/automation-proof-project-1",
                "skips_second_review": resume.get("reviewAttempted") is True
                and review_count == 1,
            }
        finally:
            store.close()


def run_proof() -> dict[str, object]:
    parallel, parallel_persisted, scheduler_evidence = _real_fixture_run(
        _PROJECTS, parallel=True
    )
    serial, serial_persisted, baseline_evidence = _real_fixture_run(
        _PROJECTS, parallel=False
    )
    swarm = _evidence(parallel, parallel_persisted)
    baseline = _evidence(serial, serial_persisted)
    restart_recovery = _restart_recovery_evidence()
    comparison: dict[str, bool] = {
        "completion_not_worse": cast(int, swarm["completed_projects"])
        >= cast(int, baseline["completed_projects"]),
        "handoffs_not_fewer": cast(int, swarm["handoff_count"])
        >= cast(int, baseline["handoff_count"]),
        "persistence_preserved": bool(swarm["handoffs_persisted"])
        and bool(baseline["handoffs_persisted"]),
        "quality_preserved": bool(swarm["quality_evidence"])
        and bool(baseline["quality_evidence"]),
        "restart_recovery_preserved": all(restart_recovery.values()),
        "scheduler_admission_preserved": scheduler_evidence["started_count"]
        == len(_PROJECTS)
        and scheduler_evidence["peak_active_count"] == len(_PROJECTS)
        and baseline_evidence["started_count"] == 0
        and baseline_evidence["peak_active_count"] == 1,
        "safety_preserved": (
            bool(swarm["handoffs_present"])
            and bool(swarm["all_handoffs_single_branch"])
            and bool(swarm["single_branch_per_project"])
            and bool(swarm["at_most_one_review_per_project"])
            and bool(swarm["all_handoffs_succeeded"])
            and bool(swarm["quality_evidence"])
            and bool(swarm["handoffs_persisted"])
            and all(restart_recovery.values())
            and bool(swarm["bounded_payload"])
            and not bool(swarm["raw_session_output"])
        ),
    }
    return {
        "schema_version": 1,
        "baseline_kind": "plain_goal_serial_fixture",
        "fixture_contract": "disposable_autonomous_lifecycle",
        "scheduler_evidence": scheduler_evidence,
        "baseline_execution_evidence": baseline_evidence,
        "baseline_disclosure": (
            "The serial baseline uses the same deterministic disposable lifecycle "
            "contract as a plain /goal run; it does not invoke a live Codex process."
        ),
        "swarm": swarm,
        "plain_goal_baseline": baseline,
        "restart_recovery": restart_recovery,
        "comparison": comparison,
        "result": "passed" if all(comparison.values()) else "failed",
    }


def main() -> int:
    proof = run_proof()
    print(json.dumps(proof, indent=2, sort_keys=True))
    return 0 if proof["result"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
