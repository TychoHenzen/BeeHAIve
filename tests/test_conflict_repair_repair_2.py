from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from beehaiive.conflict_repair import ConflictRepairService
from beehaiive.models import PullRequestSnapshot
from beehaiive.workflow import (
    Constitution,
    RepairStatus,
    WorkflowError,
    WorkflowService,
    WorkflowStore,
)
from tests.support.conflict_repair.fake_provider import FakeProvider as FakeProvider
from tests.support.conflict_repair.fixture_check import FixtureCheck as FixtureCheck
from tests.support.conflict_repair.helpers import (
    make_conflict_repository,
    pull_request_snapshot,
)
from tests.support.conflict_repair.raising_repair_agent import (
    RaisingRepairAgent as RaisingRepairAgent,
)
from tests.support.conflict_repair.repair_agent import RepairAgent as RepairAgent

CONSTITUTION_PATH = Path(__file__).parents[1] / "constitution.json"


def test_conflict_repair_unexpected_agent_error_and_cleanup_error_are_audited(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, source_head, target_head = make_conflict_repository(tmp_path)
    provider = FakeProvider(pull_request_snapshot(source_head, target_head))
    store = WorkflowStore(tmp_path / "workflow.sqlite3")
    workflow = WorkflowService(
        store,
        repository,
        Constitution.load(CONSTITUTION_PATH),
        [FixtureCheck()],
    )
    service = ConflictRepairService(
        workflow, provider, RaisingRepairAgent(), tmp_path / "repairs"
    )
    crashed = service.repair("owner/repo", 7)
    assert crashed.status is RepairStatus.AWAITING_CLARIFICATION
    assert "crashed" in (crashed.required_action or "")
    store.close()

    second_root = tmp_path / "second"
    second_root.mkdir()
    repository2, source_head2, target_head2 = make_conflict_repository(second_root)
    provider2 = FakeProvider(pull_request_snapshot(source_head2, target_head2))
    store2 = WorkflowStore(second_root / "workflow.sqlite3")
    workflow2 = WorkflowService(
        store2,
        repository2,
        Constitution.load(CONSTITUTION_PATH),
        [FixtureCheck()],
    )
    original_discard = workflow2.discard_workspace

    def broken_discard(lease_id: str, reason: str) -> object:
        del lease_id, reason
        raise WorkflowError("cleanup unavailable")

    monkeypatch.setattr(workflow2, "discard_workspace", broken_discard)
    service2 = ConflictRepairService(
        workflow2, provider2, RepairAgent(), tmp_path / "repairs-cleanup"
    )
    cleanup = service2.repair("owner/repo", 7)
    assert cleanup.status is RepairStatus.AWAITING_CLARIFICATION
    assert "cleanup" in (cleanup.required_action or "")
    monkeypatch.setattr(workflow2, "discard_workspace", original_discard)
    store2.close()


def test_conflict_repair_stale_head_becomes_operator_state(tmp_path: Path) -> None:
    repository, source_head, target_head = make_conflict_repository(tmp_path)
    provider = FakeProvider(pull_request_snapshot(source_head, target_head))
    provider.change_before_update = True
    store = WorkflowStore(tmp_path / "workflow.sqlite3")
    workflow = WorkflowService(
        store,
        repository,
        Constitution.load(CONSTITUTION_PATH),
        [FixtureCheck()],
    )
    service = ConflictRepairService(
        workflow, provider, RepairAgent(), tmp_path / "repairs"
    )

    stale = service.repair("owner/repo", 7)

    assert stale.status is RepairStatus.AWAITING_CLARIFICATION
    assert "changed" in (stale.required_action or "")
    assert provider.update_calls == 0
    store.close()


def test_conflict_repair_unknown_and_clean_evidence_are_not_repaired(
    tmp_path: Path,
) -> None:
    repository, source_head, target_head = make_conflict_repository(tmp_path)
    store = WorkflowStore(tmp_path / "workflow.sqlite3")
    workflow = WorkflowService(
        store,
        repository,
        Constitution.load(CONSTITUTION_PATH),
        [FixtureCheck()],
    )
    unknown_provider = FakeProvider(
        replace(pull_request_snapshot(source_head, target_head), merge_state="UNKNOWN")
    )
    unknown_service = ConflictRepairService(
        workflow, unknown_provider, RepairAgent(), tmp_path / "repairs"
    )
    unknown = unknown_service.repair("owner/repo", 7)
    assert unknown.status is RepairStatus.AWAITING_CLARIFICATION
    assert unknown.required_action
    assert unknown_provider.update_calls == 0

    clean_provider = FakeProvider(
        replace(
            pull_request_snapshot(source_head, target_head),
            mergeable="MERGEABLE",
            merge_state="CLEAN",
        )
    )
    clean_store = WorkflowStore(tmp_path / "workflow-clean.sqlite3")
    clean_workflow = WorkflowService(
        clean_store,
        repository,
        Constitution.load(CONSTITUTION_PATH),
        [FixtureCheck()],
    )
    clean_service = ConflictRepairService(
        clean_workflow,
        clean_provider,
        RepairAgent(),
        tmp_path / "repairs-clean",
    )
    clean = clean_service.repair("owner/repo", 7)
    assert clean.status is RepairStatus.NOT_REQUIRED
    assert clean_provider.update_calls == 0
    clean_store.close()
    store.close()


def test_workflow_store_reuses_repair_identity(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path / "workflow.sqlite3")
    first = store.begin_repair(
        "repair-1",
        "PR_7",
        "owner/repo",
        7,
        "feature",
        "target",
        "head-1",
        "base-1",
        "codex/repair-1",
        str(tmp_path / "worktree"),
        {"before": {"source_head": "head-1"}},
    )
    second = store.begin_repair(
        "repair-2",
        "PR_7",
        "owner/repo",
        7,
        "different",
        "different",
        "head-1",
        "base-2",
        "codex/repair-2",
        str(tmp_path / "different"),
        {"changed": True},
    )
    assert second == first
    assert store.get_repair_for_identity("PR_7", "head-1") == first
    assert first.as_dict()["expected_head"] == "head-1"
    store.close()


def test_pull_request_snapshot_fails_closed_for_contradictory_evidence() -> None:
    snapshot = PullRequestSnapshot(
        "owner/repo",
        7,
        "PR_7",
        "https://example.test/pull/7",
        "OPEN",
        False,
        "feature",
        "head",
        "target",
        "base",
        "CONFLICTING",
        "CLEAN",
    )
    assert snapshot.conflict_state == "unknown"
    assert snapshot.as_dict()["conflict_state"] == "unknown"

    incomplete = replace(snapshot, source_head=None, evidence_error=None)
    assert incomplete.conflict_state == "unknown"
