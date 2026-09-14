from __future__ import annotations

from pathlib import Path

import pytest

from beehaiive.workflow import (
    GateResult,
    GitWorktreeManager,
    HandoffStatus,
    WorkflowError,
    WorkflowRole,
    WorkflowStore,
)
from main import _production_workflow_service
from tests.support.workflow.fixture_check import FixtureCheck as FixtureCheck
from tests.support.workflow.helpers import commit_repository_change, make_service

CONSTITUTION_PATH = Path(__file__).parents[1] / "constitution.json"


def test_production_workflow_composition_loads_persistent_dependencies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BEEHAIIVE_WORKFLOW_DB", str(tmp_path / "production.db"))
    monkeypatch.setenv("BEEHAIIVE_WORKFLOW_REPOSITORY", str(tmp_path))

    service = _production_workflow_service()

    try:
        assert service.constitution.rules_for(WorkflowRole.WRITER)
        assert service.worktrees.repository == tmp_path.resolve()
    finally:
        service.store.close()


def test_workflow_store_rejects_corrupt_evidence_and_unknown_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = WorkflowStore(tmp_path / "corrupt.sqlite3")
    with pytest.raises(WorkflowError, match="required"):
        store.acquire_lease("", "branch", "path")
    with pytest.raises(WorkflowError, match="at most"):
        store.acquire_lease("x" * 401, "branch", "path")
    with pytest.raises(WorkflowError, match="required"):
        store.acquire_lease("agent", "branch", " ")
    with pytest.raises(WorkflowError, match="at most"):
        store.acquire_lease("agent", "branch-long", "x" * 1_001)
    with pytest.raises(WorkflowError, match="Unknown workspace lease"):
        store.release_lease("missing")
    with pytest.raises(WorkflowError, match="Unknown workspace lease"):
        store.record_handoff(
            "missing",
            WorkflowRole.WRITER,
            WorkflowRole.REVIEWER,
            "sha",
            "state",
            HandoffStatus.ACCEPTED,
            (),
            (),
            None,
        )
    with pytest.raises(WorkflowError, match="Unknown handoff"):
        store.update_handoff("missing", HandoffStatus.BLOCKED, "reason")
    with pytest.raises(WorkflowError, match="Unknown workspace lease"):
        store.record_gate("missing", GateResult("model_call", False, (), "reason"))

    lease = store.acquire_lease("agent", "branch", "path")
    monkeypatch.setattr(store, "get_lease", lambda _lease_id: None)
    with pytest.raises(WorkflowError, match="not persisted"):
        store.acquire_lease("agent-2", "branch-2", "path-2")
    with pytest.raises(WorkflowError, match="disappeared"):
        store.release_lease(lease.lease_id)
    store.close()

    persistence_store = WorkflowStore(tmp_path / "persistence.sqlite3")
    persistence_lease = persistence_store.acquire_lease("agent", "branch", "path")
    persistence_handoff = persistence_store.record_handoff(
        persistence_lease.lease_id,
        WorkflowRole.WRITER,
        WorkflowRole.REVIEWER,
        "sha",
        "state",
        HandoffStatus.ACCEPTED,
        (),
        (),
        None,
    )
    monkeypatch.setattr(persistence_store, "get_handoff", lambda _handoff_id: None)
    with pytest.raises(WorkflowError, match="not persisted"):
        persistence_store.record_handoff(
            persistence_lease.lease_id,
            WorkflowRole.WRITER,
            WorkflowRole.REVIEWER,
            "sha",
            "state",
            HandoffStatus.ACCEPTED,
            (),
            (),
            None,
        )
    with pytest.raises(WorkflowError, match="disappeared"):
        persistence_store.update_handoff(
            persistence_handoff.handoff_id, HandoffStatus.BLOCKED, "reason"
        )
    persistence_store.close()


def test_workflow_manager_and_service_report_missing_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = WorkflowStore()
    with pytest.raises(WorkflowError, match="does not exist"):
        GitWorktreeManager(tmp_path / "missing-repository", store)
    service_root = tmp_path / "service"
    service_root.mkdir()
    service, service_store, _ = make_service(service_root, [FixtureCheck("check")])
    with pytest.raises(WorkflowError, match="Unknown workspace lease"):
        service.before_model_call("missing")
    lease = service.acquire_workspace(
        "agent", "codex/service", tmp_path / "service-worktree"
    )
    with pytest.raises(WorkflowError, match="at most"):
        service.handoff(
            lease.lease_id,
            "writer",
            "reviewer",
            "sha",
            "x" * 1_001,
        )
    original_head = service.worktrees.head

    def broken_head(_worktree: str | Path) -> str:
        raise WorkflowError("head unavailable")

    monkeypatch.setattr(service.worktrees, "head", broken_head)
    commit_failure = service.handoff(
        lease.lease_id, "writer", "reviewer", "sha", "state"
    )
    assert commit_failure.status is HandoffStatus.BLOCKED
    monkeypatch.setattr(service.worktrees, "head", original_head)

    def broken_clean(_worktree: str | Path) -> bool:
        raise WorkflowError("clean check unavailable")

    monkeypatch.setattr(service.worktrees, "clean", broken_clean)
    clean_failure = service.handoff(
        lease.lease_id, "writer", "reviewer", "sha", "state"
    )
    assert clean_failure.status is HandoffStatus.BLOCKED
    service_store.close()
    store.close()


def test_failed_and_missing_evidence_pause_workflow(tmp_path: Path) -> None:
    failing_check = FixtureCheck("tests", passed=False, evidence="tests failed")
    service, store, _ = make_service(tmp_path, [failing_check])
    worktree = tmp_path / "blocked-worktree"
    lease = service.acquire_workspace("writer", "codex/blocked", worktree)
    gate = service.before_model_call(lease.lease_id)
    assert gate.allowed is False
    assert gate.required_action is not None

    blocked = service.handoff(
        lease.lease_id,
        "writer",
        "reviewer",
        "",
        "",
    )
    assert blocked.status is HandoffStatus.BLOCKED
    assert "tests" in (blocked.required_action or "")
    assert any(check.name == "commit" and not check.passed for check in blocked.checks)
    with pytest.raises(WorkflowError, match="awaiting approval"):
        service.approve_handoff(blocked.handoff_id, "operator")

    dirty_sha = commit_repository_change(worktree, "dirty.txt", "committed\n")
    (worktree / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")
    dirty = service.handoff(
        lease.lease_id,
        "writer",
        "reviewer",
        dirty_sha,
        "writer state",
    )
    assert dirty.status is HandoffStatus.BLOCKED
    assert any(
        check.name == "working_tree" and not check.passed for check in dirty.checks
    )
    store.close()
