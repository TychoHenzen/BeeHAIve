from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import FakeProvider as ProjectProviderDouble
from fastapi.testclient import TestClient

from beehaiive.conflict_repair import ConflictRepairService
from beehaiive.models import ProjectSnapshot, PullRequestSnapshot
from beehaiive.orchestrator import Orchestrator
from beehaiive.provider import ProviderError
from beehaiive.routing import AttemptOutcome, ModelExecution, ModelRouter, RoutingStore
from beehaiive.storage import OrchestratorStore
from beehaiive.workflow import (
    CheckResult,
    Constitution,
    GitWorktreeManager,
    RepairStatus,
    WorkflowError,
    WorkflowService,
    WorkflowStore,
)
from main import create_app

CONSTITUTION_PATH = Path(__file__).parents[1] / "constitution.json"


def _git(cwd: Path, *arguments: str) -> str:
    result = subprocess.run(
        ("git", *arguments),
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return result.stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, str, str]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-b", "master")
    _git(repository, "config", "user.email", "tests@example.test")
    _git(repository, "config", "user.name", "Conflict Tests")
    (repository / "README.md").write_text("base\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-m", "base")
    _git(repository, "switch", "-c", "feature")
    (repository / "README.md").write_text("source\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-m", "source")
    source_head = _git(repository, "rev-parse", "HEAD")
    _git(repository, "switch", "master")
    _git(repository, "switch", "-c", "target")
    (repository / "README.md").write_text("target\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-m", "target")
    target_head = _git(repository, "rev-parse", "HEAD")
    _git(repository, "switch", "feature")
    return repository, source_head, target_head


class FakeProvider:
    def __init__(self, snapshot: PullRequestSnapshot) -> None:
        self.snapshot = snapshot
        self.update_calls = 0
        self.get_calls = 0
        self.change_before_update = False
        self.confirm_bad = False

    def get_pull_request(self, repository: str, number: int) -> PullRequestSnapshot:
        assert repository == self.snapshot.repository
        assert number == self.snapshot.number
        self.get_calls += 1
        if self.change_before_update and self.get_calls > 1 and self.update_calls == 0:
            return replace(self.snapshot, source_head="changed-head")
        return self.snapshot

    def update_source_branch(
        self,
        snapshot: PullRequestSnapshot,
        worktree: str | Path,
        expected_head: str,
        repaired_head: str,
    ) -> None:
        del worktree
        if snapshot.source_head != expected_head:
            raise ProviderError("source head changed")
        self.update_calls += 1
        if not self.confirm_bad:
            self.snapshot = replace(
                snapshot,
                source_head=repaired_head,
                mergeable="MERGEABLE",
                merge_state="CLEAN",
            )


class RepairAgent:
    def __init__(self, *, succeed: bool = True) -> None:
        self.succeed = succeed
        self.calls = 0

    def execute_repair(
        self,
        problem_id: str,
        worktree: Path,
        source_branch: str,
        target_branch: str,
    ) -> ModelExecution:
        del problem_id, source_branch, target_branch
        self.calls += 1
        if not self.succeed:
            return ModelExecution(
                AttemptOutcome.FAILURE, failure_context="agent could not resolve"
            )
        (worktree / "README.md").write_text("resolved\n", encoding="utf-8")
        _git(worktree, "add", "README.md")
        _git(worktree, "commit", "-m", "repair conflict")
        return ModelExecution(AttemptOutcome.SUCCESS, result="resolved")


class RaisingRepairAgent(RepairAgent):
    def execute_repair(
        self,
        problem_id: str,
        worktree: Path,
        source_branch: str,
        target_branch: str,
    ) -> ModelExecution:
        del problem_id, worktree, source_branch, target_branch
        raise RuntimeError("repair process crashed")


class FailingCheck:
    name = "fixture"

    def run(self, workspace: Path) -> CheckResult:
        return CheckResult(self.name, False, "gate failed")


class FixtureCheck:
    name = "fixture"

    def run(self, workspace: Path) -> CheckResult:
        return CheckResult(self.name, workspace.exists(), "fixture checked")


def _snapshot(source_head: str, target_head: str) -> PullRequestSnapshot:
    return PullRequestSnapshot(
        repository="owner/repo",
        number=7,
        pull_request_id="PR_7",
        url="https://example.test/pull/7",
        state="OPEN",
        merged=False,
        source_branch="feature",
        source_head=source_head,
        target_branch="target",
        target_head=target_head,
        mergeable="CONFLICTING",
        merge_state="DIRTY",
    )


def test_conflict_repair_isolated_and_idempotent(tmp_path: Path) -> None:
    repository, source_head, target_head = _repository(tmp_path)
    provider = FakeProvider(_snapshot(source_head, target_head))
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
    assert _git(repository, "branch", "--show-current") == "feature"
    assert _git(repository, "rev-parse", "HEAD") == source_head
    assert (repository / "README.md").read_text(encoding="utf-8") == "source\n"
    repeated = service.repair("owner/repo", 7)
    assert repeated.status is RepairStatus.NOT_REQUIRED
    assert provider.update_calls == 1
    store.close()


def test_conflict_repair_agent_failure_is_persisted_without_update(
    tmp_path: Path,
) -> None:
    repository, source_head, target_head = _repository(tmp_path)
    provider = FakeProvider(_snapshot(source_head, target_head))
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


def test_conflict_repair_provider_failure_and_invalid_number_are_audited(
    tmp_path: Path,
) -> None:
    repository, _source_head, _target_head = _repository(tmp_path)

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
    repository, source_head, target_head = _repository(tmp_path)
    provider = FakeProvider(_snapshot(source_head, target_head))
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
    repository2, source_head2, target_head2 = _repository(second_root)
    provider2 = FakeProvider(_snapshot(source_head2, target_head2))
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


def test_conflict_repair_unexpected_agent_error_and_cleanup_error_are_audited(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, source_head, target_head = _repository(tmp_path)
    provider = FakeProvider(_snapshot(source_head, target_head))
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
    repository2, source_head2, target_head2 = _repository(second_root)
    provider2 = FakeProvider(_snapshot(source_head2, target_head2))
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
    repository, source_head, target_head = _repository(tmp_path)
    provider = FakeProvider(_snapshot(source_head, target_head))
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
    repository, source_head, target_head = _repository(tmp_path)
    store = WorkflowStore(tmp_path / "workflow.sqlite3")
    workflow = WorkflowService(
        store,
        repository,
        Constitution.load(CONSTITUTION_PATH),
        [FixtureCheck()],
    )
    unknown_provider = FakeProvider(
        replace(_snapshot(source_head, target_head), merge_state="UNKNOWN")
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
            _snapshot(source_head, target_head),
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


def test_conflict_repair_api_returns_and_reads_durable_record(tmp_path: Path) -> None:
    repository, source_head, target_head = _repository(tmp_path)
    provider = FakeProvider(
        replace(
            _snapshot(source_head, target_head),
            mergeable="MERGEABLE",
            merge_state="CLEAN",
        )
    )
    workflow_store = WorkflowStore(tmp_path / "workflow.sqlite3")
    workflow = WorkflowService(
        workflow_store,
        repository,
        Constitution.load(CONSTITUTION_PATH),
        [FixtureCheck()],
    )
    repair_service = ConflictRepairService(
        workflow, provider, RepairAgent(), tmp_path / "repairs"
    )
    project_store = OrchestratorStore()
    project_provider = ProjectProviderDouble(ProjectSnapshot("project-1", "P", ()))
    routing_store = RoutingStore()
    app = create_app(
        orchestrator=Orchestrator(project_store, project_provider),
        api_key="key",
        model_router=ModelRouter(routing_store),
        routing_store=routing_store,
        workflow_service=workflow,
        conflict_repair_service=repair_service,
    )
    try:
        with TestClient(app) as client:
            response = client.post(
                "/workflow/conflict-repairs",
                headers={"X-API-Key": "key"},
                json={"repository": "owner/repo", "pull_request_number": 7},
            )
            assert response.status_code == 200
            payload = response.json()
            assert payload["status"] == "not_required"
            read = client.get(
                f"/workflow/conflict-repairs/{payload['repair_id']}",
                headers={"X-API-Key": "key"},
            )
            assert read.status_code == 200
            assert read.json()["repair_id"] == payload["repair_id"]
            missing = client.get(
                "/workflow/conflict-repairs/missing",
                headers={"X-API-Key": "key"},
            )
            assert missing.status_code == 404
        unconfigured = create_app(
            orchestrator=Orchestrator(project_store, project_provider),
            api_key="key",
            model_router=ModelRouter(routing_store),
            routing_store=routing_store,
            workflow_service=workflow,
        )
        with TestClient(unconfigured) as client:
            response = client.post(
                "/workflow/conflict-repairs",
                headers={"X-API-Key": "key"},
                json={"repository": "owner/repo", "pull_request_number": 7},
            )
            assert response.status_code == 503
            read = client.get(
                "/workflow/conflict-repairs/missing",
                headers={"X-API-Key": "key"},
            )
            assert read.status_code == 503
    finally:
        workflow_store.close()
        project_store.close()
        routing_store.close()


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


def test_workflow_store_rejects_invalid_repair_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = WorkflowStore(tmp_path / "workflow.sqlite3")
    evidence = {
        "before": {"source_head": "head-1"},
    }
    try:
        with pytest.raises(WorkflowError, match="positive"):
            store.begin_repair(
                "invalid-number",
                "PR_invalid",
                "owner/repo",
                0,
                "feature",
                "target",
                "head-1",
                "base-1",
                "codex/repair-invalid",
                str(tmp_path / "invalid"),
                evidence,
            )
        record = store.begin_repair(
            "repair-state",
            "PR_state",
            "owner/repo",
            7,
            "feature",
            "target",
            "head-state",
            "base-state",
            "codex/repair-state",
            str(tmp_path / "state"),
            evidence,
        )
        with store._transaction() as connection:
            connection.execute(
                "UPDATE workflow_repairs SET evidence_json = ? WHERE repair_id = ?",
                ("not-json", record.repair_id),
            )
        with pytest.raises(WorkflowError, match="evidence"):
            store.get_repair(record.repair_id)

        with store._transaction() as connection:
            connection.execute(
                "UPDATE workflow_repairs SET evidence_json = ? WHERE repair_id = ?",
                ("[]", record.repair_id),
            )
        with pytest.raises(WorkflowError, match="evidence"):
            store.get_repair(record.repair_id)

        with store._transaction() as connection:
            connection.execute(
                "UPDATE workflow_repairs SET evidence_json = ?, status = ? "
                "WHERE repair_id = ?",
                ("{}", "invalid", record.repair_id),
            )
        with pytest.raises(WorkflowError, match="status"):
            store.get_repair(record.repair_id)

        with store._transaction() as connection:
            connection.execute(
                "UPDATE workflow_repairs SET status = ? WHERE repair_id = ?",
                (RepairStatus.RUNNING.value, record.repair_id),
            )
        with pytest.raises(WorkflowError, match="Unknown repair"):
            store.attach_repair_workspace("missing", "lease", str(tmp_path / "missing"))
        attached = store.attach_repair_workspace(
            record.repair_id, "lease-1", str(tmp_path / "state-worktree")
        )
        assert attached.lease_id == "lease-1"
        with pytest.raises(WorkflowError, match="different workspace"):
            store.attach_repair_workspace(
                record.repair_id, "lease-2", str(tmp_path / "other-worktree")
            )

        disappearing = store.begin_repair(
            "repair-disappears",
            "PR_disappears",
            "owner/repo",
            8,
            "feature",
            "target",
            "head-disappears",
            "base-disappears",
            "codex/repair-disappears",
            str(tmp_path / "disappears"),
            {},
        )
        monkeypatch.setattr(store, "get_repair", lambda repair_id: None)
        with pytest.raises(WorkflowError, match="disappeared"):
            store.attach_repair_workspace(
                disappearing.repair_id, "lease-disappears", str(tmp_path / "gone")
            )
        monkeypatch.undo()

        finished = store.finish_repair(
            record.repair_id,
            RepairStatus.SUCCEEDED,
            None,
            {"finished": True},
            repaired_head="head-fixed",
        )
        assert finished.status is RepairStatus.SUCCEEDED
        assert (
            store.finish_repair(
                record.repair_id,
                RepairStatus.BLOCKED,
                "ignored",
                {"ignored": True},
            )
            == finished
        )
        with pytest.raises(WorkflowError, match="Unknown repair"):
            store.finish_repair("missing", RepairStatus.BLOCKED, "missing", {})

        finishing_disappears = store.begin_repair(
            "repair-finishing-disappears",
            "PR_finishing-disappears",
            "owner/repo",
            9,
            "feature",
            "target",
            "head-finishing-disappears",
            "base-finishing-disappears",
            "codex/repair-finishing-disappears",
            str(tmp_path / "finishing-disappears"),
            {},
        )
        monkeypatch.setattr(store, "get_repair", lambda repair_id: None)
        with pytest.raises(WorkflowError, match="disappeared"):
            store.finish_repair(
                finishing_disappears.repair_id,
                RepairStatus.BLOCKED,
                "gone",
                {},
            )
        monkeypatch.undo()

        with store._transaction() as connection:
            connection.execute(
                """
                CREATE TRIGGER delete_repair_after_insert
                AFTER INSERT ON workflow_repairs
                BEGIN
                    DELETE FROM workflow_repairs WHERE repair_id = NEW.repair_id;
                END
                """
            )
        with pytest.raises(WorkflowError, match="persisted"):
            store.begin_repair(
                "repair-not-persisted",
                "PR_not-persisted",
                "owner/repo",
                10,
                "feature",
                "target",
                "head-not-persisted",
                "base-not-persisted",
                "codex/repair-not-persisted",
                str(tmp_path / "not-persisted"),
                {},
            )
    finally:
        store.close()


def test_worktree_manager_proves_exact_refs_and_merge_results(tmp_path: Path) -> None:
    repository, source_head, target_head = _repository(tmp_path)
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", str(remote))
    _git(repository, "remote", "add", "origin", str(remote))
    _git(repository, "push", "origin", "--all")
    store = WorkflowStore(tmp_path / "workflow.sqlite3")
    manager = GitWorktreeManager(repository, store)
    source_ref = "refs/beehaiive/tests/source"
    try:
        assert (
            manager.fetch_exact_branch("feature", source_head, source_ref) == source_ref
        )
        with pytest.raises(WorkflowError, match="changed"):
            manager.fetch_exact_branch("feature", "different-head", source_ref)
        with pytest.raises(WorkflowError, match="remote ref"):
            manager.fetch_exact_branch("missing", source_head, source_ref)
        manager.remove_ref(source_ref)
        manager.remove_ref("")

        _git(repository, "remote", "remove", "origin")
        with pytest.raises(WorkflowError, match="not available"):
            manager.fetch_exact_branch("feature", "missing-local-head", source_ref)
        assert (
            manager.fetch_exact_branch("feature", source_head, source_ref) == source_ref
        )

        worktree = tmp_path / "manager-worktree"
        lease = manager.acquire(
            "manager-test", "codex/manager-test", worktree, source_head
        )
        assert (
            manager.integrate_target(worktree, source_head, source_head).conflicted
            is False
        )
        with pytest.raises(WorkflowError, match="expected head"):
            manager.integrate_target(worktree, "wrong-head", source_head)
        with pytest.raises(WorkflowError, match="missing-target"):
            manager.integrate_target(worktree, source_head, "missing-target")
        manager.release(lease.lease_id)

        workflow = WorkflowService(
            store,
            repository,
            Constitution.load(CONSTITUTION_PATH),
            [FixtureCheck()],
            worktrees=manager,
        )
        with pytest.raises(WorkflowError, match="Unknown workspace lease"):
            workflow.discard_workspace("missing-lease", "test")
    finally:
        manager.remove_ref(source_ref)
        store.close()
