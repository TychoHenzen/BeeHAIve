from __future__ import annotations

from pathlib import Path

import pytest

from beehaiive.conflict_repair import ConflictRepairService
from beehaiive.models import PullRequestSnapshot
from beehaiive.provider import ProviderError
from beehaiive.workflow import (
    Constitution,
    RepairStatus,
    WorkflowError,
    WorkflowService,
    WorkflowStore,
)
from tests.support.conflict_repair.failing_check import FailingCheck as FailingCheck
from tests.support.conflict_repair.fake_provider import FakeProvider as FakeProvider
from tests.support.conflict_repair.fixture_check import FixtureCheck as FixtureCheck
from tests.support.conflict_repair.helpers import (
    git_command,
    make_conflict_repository,
    pull_request_snapshot,
)
from tests.support.conflict_repair.non_merging_repair_agent import (
    NonMergingRepairAgent as NonMergingRepairAgent,
)
from tests.support.conflict_repair.repair_agent import RepairAgent as RepairAgent

CONSTITUTION_PATH = Path(__file__).parents[1] / "constitution.json"


def test_conflict_repair_isolated_and_idempotent(tmp_path: Path) -> None:
    repository, source_head, target_head = make_conflict_repository(tmp_path)
    provider = FakeProvider(pull_request_snapshot(source_head, target_head))
    agent = RepairAgent()
    store = WorkflowStore(tmp_path / "workflow.sqlite3")
    workflow = WorkflowService(
        store,
        repository,
        Constitution.load(CONSTITUTION_PATH),
        [FixtureCheck()],
    )
    service = ConflictRepairService(workflow, provider, agent, tmp_path / "repairs")

    repaired = service.repair("owner/repo", 7)

    assert repaired.status is RepairStatus.SUCCEEDED
    assert repaired.repaired_head
    assert provider.update_calls == 1
    assert agent.calls == 1
    assert all(check.passed for check in repaired.checks)
    assert not Path(repaired.worktree_path).exists()
    assert git_command(repository, "branch", "--show-current") == "feature"
    assert git_command(repository, "rev-parse", "HEAD") == source_head
    assert (repository / "README.md").read_text(encoding="utf-8") == "source\n"
    repeated = service.repair("owner/repo", 7)
    assert repeated.status is RepairStatus.NOT_REQUIRED
    assert provider.update_calls == 1
    store.close()


def test_conflict_repair_agent_failure_is_persisted_without_update(
    tmp_path: Path,
) -> None:
    repository, source_head, target_head = make_conflict_repository(tmp_path)
    provider = FakeProvider(pull_request_snapshot(source_head, target_head))
    agent = RepairAgent(succeed=False)
    store = WorkflowStore(tmp_path / "workflow.sqlite3")
    workflow = WorkflowService(
        store,
        repository,
        Constitution.load(CONSTITUTION_PATH),
        [FixtureCheck()],
    )
    service = ConflictRepairService(workflow, provider, agent, tmp_path / "repairs")

    failed = service.repair("owner/repo", 7)

    assert failed.status is RepairStatus.BLOCKED
    assert failed.required_action == "agent could not resolve"
    assert provider.update_calls == 0
    assert not Path(failed.worktree_path).exists()
    repeated = service.repair("owner/repo", 7)
    assert repeated.repair_id == failed.repair_id
    assert agent.calls == 1
    store.close()


def test_conflict_repair_requires_the_target_head_in_the_result(
    tmp_path: Path,
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
        workflow, provider, NonMergingRepairAgent(), tmp_path / "repairs"
    )

    blocked = service.repair("owner/repo", 7)

    assert blocked.status is RepairStatus.AWAITING_CLARIFICATION
    assert blocked.required_action == "Repair commit does not include the target head"
    assert provider.update_calls == 0
    store.close()


def test_conflict_repair_provider_failure_and_invalid_number_are_audited(
    tmp_path: Path,
) -> None:
    repository, _source_head, _target_head = make_conflict_repository(tmp_path)

    class FailingProvider:
        def get_pull_request(self, repository: str, number: int) -> PullRequestSnapshot:
            del repository, number
            raise ProviderError("rate limited")

    store = WorkflowStore(tmp_path / "workflow.sqlite3")
    workflow = WorkflowService(
        store,
        repository,
        Constitution.load(CONSTITUTION_PATH),
        [FixtureCheck()],
    )
    service = ConflictRepairService(
        workflow, FailingProvider(), RepairAgent(), tmp_path / "repairs"
    )

    with pytest.raises(WorkflowError, match="positive"):
        service.repair("owner/repo", 0)
    failed = service.repair("owner/repo", 7)
    repeated = service.repair("owner/repo", 7)

    assert failed.status is RepairStatus.AWAITING_CLARIFICATION
    assert failed.evidence["provider_error"] == "rate limited"
    assert repeated.repair_id == failed.repair_id
    store.close()


def test_conflict_repair_gate_and_readback_failures_stop_before_completion(
    tmp_path: Path,
) -> None:
    repository, source_head, target_head = make_conflict_repository(tmp_path)
    provider = FakeProvider(pull_request_snapshot(source_head, target_head))
    store = WorkflowStore(tmp_path / "workflow.sqlite3")
    workflow = WorkflowService(
        store,
        repository,
        Constitution.load(CONSTITUTION_PATH),
        [FailingCheck()],
    )
    gate_service = ConflictRepairService(
        workflow, provider, RepairAgent(), tmp_path / "repairs-gate"
    )
    blocked = gate_service.repair("owner/repo", 7)
    assert blocked.status is RepairStatus.BLOCKED
    assert provider.update_calls == 0
    store.close()

    second_root = tmp_path / "second"
    second_root.mkdir()
    repository2, source_head2, target_head2 = make_conflict_repository(second_root)
    provider2 = FakeProvider(pull_request_snapshot(source_head2, target_head2))
    provider2.confirm_bad = True
    store2 = WorkflowStore(second_root / "workflow.sqlite3")
    workflow2 = WorkflowService(
        store2,
        repository2,
        Constitution.load(CONSTITUTION_PATH),
        [FixtureCheck()],
    )
    readback_service = ConflictRepairService(
        workflow2, provider2, RepairAgent(), tmp_path / "repairs-readback"
    )
    unconfirmed = readback_service.repair("owner/repo", 7)
    assert unconfirmed.status is RepairStatus.AWAITING_CLARIFICATION
    assert "readback" in (unconfirmed.required_action or "")
    store2.close()
