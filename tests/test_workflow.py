from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

import pytest
from fastapi.testclient import TestClient

from beehaiive.workflow import (
    CheckResult,
    CommandCheck,
    Constitution,
    DeterministicCheckRunner,
    GateResult,
    GitWorktreeManager,
    HandoffStatus,
    LeaseStatus,
    WorkflowError,
    WorkflowRole,
    WorkflowService,
    WorkflowStore,
    _checks_from_json,
    _lease_is_expired,
)
from main import _production_workflow_service, create_app

CONSTITUTION_PATH = Path(__file__).parents[1] / "constitution.json"


@dataclass
class FixtureCheck:
    name: str
    passed: bool = True
    evidence: str = "fixture passed"
    calls: int = 0

    def run(self, workspace: Path) -> CheckResult:
        assert workspace.exists()
        self.calls += 1
        return CheckResult(self.name, self.passed, self.evidence)


class RaisingCheck:
    name = "raising"

    def run(self, workspace: Path) -> CheckResult:
        del workspace
        raise RuntimeError("fixture exploded")


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


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-b", "master")
    _git(repository, "config", "user.email", "tests@example.test")
    _git(repository, "config", "user.name", "Workflow Tests")
    (repository / "README.md").write_text("base\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-m", "base")
    return repository


def _commit(worktree: Path, name: str, content: str) -> str:
    (worktree / name).write_text(content, encoding="utf-8")
    _git(worktree, "add", name)
    _git(worktree, "commit", "-m", f"add {name}")
    return _git(worktree, "rev-parse", "HEAD")


def _service(
    tmp_path: Path,
    checks: list[FixtureCheck] | None = None,
) -> tuple[WorkflowService, WorkflowStore, Path]:
    repository = _repository(tmp_path)
    store = WorkflowStore(tmp_path / "workflow.sqlite3")
    service = WorkflowService(
        store,
        repository,
        Constitution.load(CONSTITUTION_PATH),
        checks or [FixtureCheck("tests")],
    )
    return service, store, repository


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


def test_constitution_and_check_runner_validate_evidence(tmp_path: Path) -> None:
    constitution = Constitution.load(CONSTITUTION_PATH)
    writer_rules = constitution.rules_for(WorkflowRole.WRITER)
    assert writer_rules == constitution.rules_for("writer")
    assert len(writer_rules) == 6
    with pytest.raises(TypeError):
        constitution.sections["project"] = ()  # type: ignore[index]
    with pytest.raises(TypeError):
        constitution.roles[WorkflowRole.WRITER] = ()  # type: ignore[index]
    with pytest.raises(WorkflowError, match="Unknown workflow role"):
        constitution.rules_for("unknown")

    passing = FixtureCheck("pass")
    empty = FixtureCheck("empty", evidence=" ")

    class MismatchCheck:
        name = "mismatch"

        def run(self, workspace: Path) -> CheckResult:
            del workspace
            return CheckResult("other", True, "wrong name")

    results = DeterministicCheckRunner(
        [passing, empty, MismatchCheck(), RaisingCheck()]
    ).run(tmp_path)
    assert results[0].passed is True
    assert results[1].passed is False
    assert results[2].evidence.startswith("Check failed to run")
    assert results[3].evidence.startswith("Check failed to run")

    with pytest.raises(WorkflowError, match="At least one"):
        DeterministicCheckRunner([])
    with pytest.raises(WorkflowError, match="needs a name"):
        DeterministicCheckRunner([FixtureCheck(" ")])
    with pytest.raises(WorkflowError, match="unique"):
        DeterministicCheckRunner([FixtureCheck("same"), FixtureCheck("same")])

    command_ok = CommandCheck("command", (sys.executable, "-c", "print('ok')"))
    command_fail = CommandCheck(
        "command-fail", (sys.executable, "-c", "import sys; sys.exit(3)")
    )
    assert command_ok.run(tmp_path).passed is True
    assert command_fail.run(tmp_path).passed is False
    with pytest.raises(WorkflowError, match="no command"):
        CommandCheck("empty", ()).run(tmp_path)


def test_constitution_rejects_invalid_documents(tmp_path: Path) -> None:
    with pytest.raises(WorkflowError, match="Cannot load"):
        Constitution.load(tmp_path / "missing.json")
    invalid_json = tmp_path / "invalid.json"
    invalid_json.write_text("{", encoding="utf-8")
    with pytest.raises(WorkflowError, match="Cannot load"):
        Constitution.load(invalid_json)

    cases = [
        ("not-object", [], "JSON object"),
        ("bad-version", {"version": 0}, "version"),
        ("bad-sections", {"version": 1, "sections": []}, "sections"),
        (
            "empty-rules",
            {"version": 1, "sections": {"project": []}},
            "non-empty",
        ),
        (
            "non-string-rule",
            {"version": 1, "sections": {"project": [1]}},
            "strings",
        ),
        (
            "empty-rule",
            {"version": 1, "sections": {"project": [""]}},
            "required",
        ),
        (
            "long-rule",
            {"version": 1, "sections": {"project": ["x" * 1_001]}},
            "at most",
        ),
        (
            "bad-roles",
            {"version": 1, "sections": {"project": ["rule"]}, "roles": []},
            "roles",
        ),
        (
            "unknown-role",
            {
                "version": 1,
                "sections": {"project": ["rule"]},
                "roles": {"unknown": ["project"]},
            },
            "Unknown workflow role",
        ),
        (
            "unknown-section",
            {
                "version": 1,
                "sections": {"project": ["rule"]},
                "roles": {"writer": ["engineering"]},
            },
            "unknown section",
        ),
    ]
    for name, document, message in cases:
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        with pytest.raises(WorkflowError, match=message):
            Constitution.load(path)

    missing_role = tmp_path / "missing-role.json"
    missing_role.write_text(
        json.dumps(
            {
                "version": 1,
                "sections": {"project": ["rule"]},
                "roles": {"planner": ["project"]},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(WorkflowError, match="No constitution rules"):
        Constitution.load(missing_role).rules_for("writer")

    blank_section = tmp_path / "blank-section.json"
    blank_section.write_text(
        json.dumps(
            {
                "version": 1,
                "sections": {"": ["rule"]},
                "roles": {"planner": [""]},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(WorkflowError, match="section names"):
        Constitution.load(blank_section)


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


def test_workflow_store_rejects_corrupt_handoff_json(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path / "evidence.sqlite3")
    lease = store.acquire_lease("agent", "branch", "path")
    handoff = store.record_handoff(
        lease.lease_id,
        WorkflowRole.WRITER,
        WorkflowRole.REVIEWER,
        "sha",
        "state",
        HandoffStatus.ACCEPTED,
        (CheckResult("check", True, "ok"),),
        ("rule",),
        None,
    )
    corruptions = [
        ("checks_json", "{"),
        ("checks_json", "{}"),
        ("checks_json", "[1]"),
        ("checks_json", '[{"name": "check"}]'),
        ("status", "unknown"),
    ]
    with pytest.raises(WorkflowError, match="Stored verification evidence"):
        _checks_from_json(None)
    for column, value in corruptions:
        with store._transaction() as connection:
            connection.execute(
                f"UPDATE workflow_handoffs SET {column} = ? WHERE handoff_id = ?",
                (value, handoff.handoff_id),
            )
        with pytest.raises(WorkflowError):
            store.get_handoff(handoff.handoff_id)
    store.close()


def test_workflow_store_migrates_legacy_lease_liveness_columns(
    tmp_path: Path,
) -> None:
    database = tmp_path / "legacy-leases.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute(
        """
        CREATE TABLE workflow_leases(
            lease_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL,
            branch TEXT NOT NULL, worktree_path TEXT NOT NULL,
            status TEXT NOT NULL, stop_reason TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        INSERT INTO workflow_leases(
            lease_id, agent_id, branch, worktree_path, status,
            stop_reason, created_at, updated_at
        ) VALUES ('legacy', 'agent', 'branch', 'path', 'active', NULL, 'now', 'now')
        """
    )
    connection.commit()
    connection.close()

    store = WorkflowStore(database)

    try:
        lease = store.get_lease("legacy")
        assert lease is not None
        assert lease.lease_token
        assert lease.expires_at
    finally:
        store.close()


def test_workflow_manager_and_service_report_missing_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = WorkflowStore()
    with pytest.raises(WorkflowError, match="does not exist"):
        GitWorktreeManager(tmp_path / "missing-repository", store)
    service_root = tmp_path / "service"
    service_root.mkdir()
    service, service_store, _ = _service(service_root, [FixtureCheck("check")])
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


def test_workflow_rejects_non_topological_role_handoffs(tmp_path: Path) -> None:
    service, store, _ = _service(tmp_path)
    worktree = tmp_path / "role-worktree"
    lease = service.acquire_workspace("writer", "codex/role", worktree)

    with pytest.raises(WorkflowError, match="not permitted"):
        service.handoff(
            lease.lease_id,
            WorkflowRole.WRITER,
            WorkflowRole.PLANNER,
            "sha",
            "state",
        )
    assert store.latest_handoff(lease.lease_id) is None
    service.release_workspace(lease.lease_id)
    store.close()


def test_handoff_updates_use_compare_and_set_status(tmp_path: Path) -> None:
    service, store, repository = _service(tmp_path)
    worktree = tmp_path / "cas-worktree"
    lease = service.acquire_workspace("writer", "codex/cas", worktree)
    commit_sha = _commit(worktree, "cas.txt", "cas\n")
    waiting = service.handoff(
        lease.lease_id,
        WorkflowRole.WRITER,
        WorkflowRole.OPERATOR,
        commit_sha,
        "ready",
    )

    stopped = store.update_handoff(
        waiting.handoff_id,
        HandoffStatus.STOPPED,
        "operator stop",
        expected_status=HandoffStatus.AWAITING_APPROVAL,
    )
    assert stopped.status is HandoffStatus.STOPPED
    with pytest.raises(WorkflowError, match="transition conflict"):
        store.update_handoff(
            waiting.handoff_id,
            HandoffStatus.ACCEPTED,
            None,
            expected_status=HandoffStatus.AWAITING_APPROVAL,
        )
    manager = service.worktrees
    manager._cleanup_stopped_worktree(str(worktree))
    store.release_lease(lease.lease_id, allow_stopped=True)
    store.close()
    assert repository.exists()


def test_worktree_path_identity_preserves_internal_whitespace(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    store = WorkflowStore()
    manager = GitWorktreeManager(repository, store)
    worktree = tmp_path / "worktree  with  spaces"

    lease = manager.acquire("agent", "codex/spaces", worktree)

    assert lease.worktree_path == str(worktree.resolve())
    released = manager.release(lease.lease_id)
    assert released.status is LeaseStatus.RELEASED
    store.close()


def test_worktree_leases_prevent_concurrent_duplicate_writes(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    store = WorkflowStore()
    manager = GitWorktreeManager(repository, store)
    barrier = Barrier(2)

    def acquire(index: int) -> object:
        barrier.wait()
        try:
            return manager.acquire(
                f"agent-{index}",
                "codex/shared",
                tmp_path / f"worktree-{index}",
            )
        except WorkflowError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(acquire, (1, 2)))
    leases = [result for result in results if not isinstance(result, WorkflowError)]
    errors = [result for result in results if isinstance(result, WorkflowError)]
    assert len(leases) == 1
    assert len(errors) == 1
    lease = leases[0]
    assert lease.status is LeaseStatus.ACTIVE
    assert manager.clean(lease.worktree_path) is True
    released = manager.release(lease.lease_id)
    assert released.status is LeaseStatus.RELEASED

    bad_base_path = tmp_path / "bad-base"
    with pytest.raises(WorkflowError):
        manager.acquire("agent-bad", "codex/bad", bad_base_path, "missing-ref")
    bad_lease = store.acquire_lease("agent-bad", "codex/bad", str(bad_base_path))
    assert store.release_lease(bad_lease.lease_id).status is LeaseStatus.RELEASED

    with pytest.raises(WorkflowError, match="Unknown workspace"):
        manager.release("missing")
    with pytest.raises(WorkflowError):
        manager.head(tmp_path / "missing-worktree")
    with pytest.raises(WorkflowError):
        manager.clean(tmp_path / "missing-worktree")
    store.close()


def test_expired_leases_are_fenced_renewed_and_reclaimed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(WorkflowError, match="TTL"):
        WorkflowStore(lease_ttl_seconds=0)
    assert _lease_is_expired(None) is True
    assert _lease_is_expired("not-a-timestamp") is True
    repository = _repository(tmp_path)
    store = WorkflowStore(tmp_path / "leases.sqlite3")
    manager = GitWorktreeManager(repository, store)
    worktree = tmp_path / "expired-worktree"
    lease = manager.acquire("agent", "codex/expired", worktree)

    with pytest.raises(WorkflowError, match="invalid"):
        store.renew_lease(lease.lease_id, "wrong-token")
    renewed = store.renew_lease(lease.lease_id, lease.lease_token)
    assert renewed.expires_at != lease.expires_at
    with store._transaction() as connection:
        connection.execute(
            "UPDATE workflow_leases SET expires_at = ? WHERE lease_id = ?",
            (
                (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
                lease.lease_id,
            ),
        )

    replacement = manager.acquire("replacement", "codex/expired", worktree)

    assert store.get_lease(lease.lease_id).status is LeaseStatus.STOPPED  # type: ignore[union-attr]
    assert replacement.status is LeaseStatus.ACTIVE
    assert replacement.lease_token != lease.lease_token
    with pytest.raises(WorkflowError, match="Unknown workspace lease"):
        store.require_lease_token("missing", None)
    with pytest.raises(WorkflowError, match="Workspace lease is stopped"):
        store.require_lease_token(lease.lease_id, lease.lease_token)
    with pytest.raises(WorkflowError, match="Unknown workspace lease"):
        store.ensure_release_allowed("missing")
    with pytest.raises(WorkflowError, match="Workspace lease is stopped"):
        store.ensure_release_allowed(lease.lease_id)
    with pytest.raises(WorkflowError, match="Unknown workspace lease"):
        store.renew_lease("missing", None)
    with pytest.raises(WorkflowError, match="Workspace lease is stopped"):
        store.renew_lease(lease.lease_id, lease.lease_token)
    disposable = store.acquire_lease("disposable", "codex/disposable", "path")
    original_get_lease = store.get_lease
    monkeypatch.setattr(store, "get_lease", lambda _lease_id: None)
    with pytest.raises(WorkflowError, match="disappeared"):
        store.ensure_release_allowed(disposable.lease_id)
    with pytest.raises(WorkflowError, match="disappeared"):
        store.renew_lease(disposable.lease_id, disposable.lease_token)
    monkeypatch.setattr(store, "get_lease", original_get_lease)
    released = manager.release(replacement.lease_id)
    assert released.status is LeaseStatus.RELEASED
    store.close()


def test_stopped_lease_cleanup_is_idempotent(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    store = WorkflowStore(tmp_path / "stopped.sqlite3")
    manager = GitWorktreeManager(repository, store)
    worktree = tmp_path / "stopped-worktree"
    lease = manager.acquire("agent", "codex/stopped", worktree)

    stopped = store.stop_lease(lease.lease_id, "operator stop")
    assert stopped.status is LeaseStatus.STOPPED
    assert store.release_lease(lease.lease_id).status is LeaseStatus.STOPPED
    with pytest.raises(WorkflowError, match="Workspace lease is stopped"):
        store.record_handoff(
            lease.lease_id,
            WorkflowRole.WRITER,
            WorkflowRole.REVIEWER,
            "sha",
            "state",
            HandoffStatus.ACCEPTED,
            (),
            (),
            None,
        )
    released = manager.release(lease.lease_id)
    repeated = manager.release(lease.lease_id)
    assert store.release_lease(lease.lease_id).status is LeaseStatus.RELEASED

    assert released.status is LeaseStatus.RELEASED
    assert repeated.status is LeaseStatus.RELEASED
    assert not worktree.exists()
    manager._delete_reclaimed_branch("codex/missing")
    with pytest.raises(WorkflowError, match="checked-out branch"):
        manager._delete_reclaimed_branch("master")
    store.close()


def test_git_timeout_is_reported_and_failed_acquisition_releases_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _repository(tmp_path)
    store = WorkflowStore()
    with pytest.raises(WorkflowError, match="Git timeout"):
        GitWorktreeManager(repository, store, git_timeout_seconds=0)
    manager = GitWorktreeManager(repository, store, git_timeout_seconds=0.25)

    def timeout(*arguments: object, **kwargs: object) -> object:
        assert kwargs["timeout"] == 0.25
        raise subprocess.TimeoutExpired("git", 0.25)

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(WorkflowError, match="timed out"):
        manager.head(tmp_path / "missing")
    with pytest.raises(WorkflowError, match="timed out"):
        manager.acquire("agent", "codex/timeout", tmp_path / "timeout-worktree")
    leases = store.get_lease("missing")
    assert leases is None
    store.close()


def test_handoffs_store_checks_commits_and_approval_states(tmp_path: Path) -> None:
    approval_check = FixtureCheck("tests")
    service, store, repository = _service(tmp_path, [approval_check])
    worktree = tmp_path / "writer-worktree"
    lease = service.acquire_workspace("writer-1", "codex/writer-1", worktree)
    gate = service.before_model_call(lease.lease_id)
    assert gate.allowed is True
    commit_sha = _commit(worktree, "change.txt", "change\n")

    accepted = service.handoff(
        lease.lease_id,
        "writer",
        "reviewer",
        commit_sha,
        "implementation complete",
    )
    assert accepted.status is HandoffStatus.ACCEPTED
    assert accepted.commit_sha == commit_sha
    assert accepted.constitution_rules
    assert {check.name for check in accepted.checks} == {
        "tests",
        "source_state",
        "constitution",
        "commit",
        "working_tree",
    }
    with pytest.raises(WorkflowError, match="cannot be paused"):
        service.request_clarification(accepted.handoff_id, "not now")
    reloaded = WorkflowStore(tmp_path / "workflow.sqlite3")
    assert reloaded.get_handoff(accepted.handoff_id) == accepted
    reloaded.close()

    approval_worktree = tmp_path / "approval-worktree"
    approval_lease = service.acquire_workspace(
        "writer-2", "codex/writer-2", approval_worktree
    )
    approval_sha = _commit(approval_worktree, "approval.txt", "approval\n")
    waiting = service.handoff(
        approval_lease.lease_id,
        WorkflowRole.WRITER,
        WorkflowRole.OPERATOR,
        approval_sha,
        "needs operator",
        approval_required=False,
    )
    assert waiting.status is HandoffStatus.AWAITING_APPROVAL
    with pytest.raises(WorkflowError, match="unresolved handoff"):
        store.release_lease(approval_lease.lease_id)
    approval_check.passed = False
    blocked_approval = service.approve_handoff(waiting.handoff_id, "operator-1")
    assert blocked_approval.status is HandoffStatus.BLOCKED
    approval_check.passed = True
    waiting = service.handoff(
        approval_lease.lease_id,
        WorkflowRole.WRITER,
        WorkflowRole.OPERATOR,
        approval_sha,
        "needs operator again",
        approval_required=True,
    )
    approved = service.approve_handoff(waiting.handoff_id, "operator-1", "reviewed")
    assert approved.status is HandoffStatus.ACCEPTED
    assert approved.approval_actor == "operator-1"
    assert approved.approval_note == "reviewed"

    service.release_workspace(lease.lease_id)
    service.release_workspace(approval_lease.lease_id)
    assert repository.exists()
    store.close()


def test_failed_and_missing_evidence_pause_workflow(tmp_path: Path) -> None:
    failing_check = FixtureCheck("tests", passed=False, evidence="tests failed")
    service, store, _ = _service(tmp_path, [failing_check])
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

    dirty_sha = _commit(worktree, "dirty.txt", "committed\n")
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


def test_clarification_stop_and_release_states(tmp_path: Path) -> None:
    service, store, _ = _service(tmp_path)
    clarification_worktree = tmp_path / "clarification-worktree"
    lease = service.acquire_workspace(
        "writer", "codex/clarification", clarification_worktree
    )
    commit_sha = _commit(clarification_worktree, "clarify.txt", "clarify\n")
    waiting = service.handoff(
        lease.lease_id,
        "writer",
        "operator",
        commit_sha,
        "waiting",
        approval_required=True,
    )
    clarified = service.request_clarification(waiting.handoff_id, "Which scope?")
    assert clarified.status is HandoffStatus.AWAITING_CLARIFICATION
    answered = service.answer_clarification(waiting.handoff_id, "The API only")
    assert answered.status is HandoffStatus.BLOCKED
    assert "API only" in (answered.required_action or "")
    with pytest.raises(WorkflowError, match="not awaiting clarification"):
        service.answer_clarification(waiting.handoff_id, "again")
    with pytest.raises(WorkflowError, match="awaiting approval"):
        service.approve_handoff(waiting.handoff_id, "operator")

    stopped = service.stop(lease.lease_id, "Operator paused the run")
    assert stopped.status is LeaseStatus.STOPPED
    assert service.get_handoff(waiting.handoff_id).status is HandoffStatus.STOPPED
    with pytest.raises(WorkflowError, match="Workspace lease is stopped"):
        service.before_model_call(lease.lease_id)

    release_worktree = tmp_path / "release-worktree"
    release_lease = service.acquire_workspace(
        "writer-2", "codex/release", release_worktree
    )
    assert (
        service.release_workspace(release_lease.lease_id).status is LeaseStatus.RELEASED
    )
    assert (
        service.release_workspace(release_lease.lease_id).status is LeaseStatus.RELEASED
    )
    with pytest.raises(WorkflowError, match="Unknown handoff"):
        service.get_handoff("missing")
    store.close()


def test_workflow_api_requires_server_configured_operator(tmp_path: Path) -> None:
    service, store, _ = _service(tmp_path)
    client = TestClient(
        create_app(
            workflow_service=service,
            api_key="test-key",
            workflow_actor=WorkflowRole.WRITER,
        )
    )

    response = client.post(
        "/workflow/handoffs/missing/approve",
        json={"note": "caller cannot choose the actor"},
        headers={"X-API-Key": "test-key"},
    )

    assert response.status_code == 403
    store.close()


def test_workflow_api_rejects_missing_or_invalid_operator_configuration(
    tmp_path: Path,
) -> None:
    for actor in (None, "not-a-role"):
        service_root = tmp_path / (actor or "missing")
        service_root.mkdir()
        service, store, _ = _service(service_root)
        client = TestClient(
            create_app(
                workflow_service=service,
                api_key="test-key",
                workflow_actor=actor,
            )
        )
        response = client.post(
            "/workflow/handoffs/missing/approve",
            json={"note": "operator decision"},
            headers={"X-API-Key": "test-key"},
        )
        assert response.status_code == 503
        store.close()


def test_workflow_api_exposes_gates_and_operator_controls(tmp_path: Path) -> None:
    service, store, _ = _service(tmp_path)
    client = TestClient(
        create_app(
            workflow_service=service,
            api_key="test-key",
            workflow_actor="operator",
        )
    )
    auth = {"X-API-Key": "test-key"}
    no_auth = client.post("/workflow/model-calls", json={"lease_id": "missing"})
    assert no_auth.status_code == 401

    worktree = tmp_path / "api-worktree"
    acquired = client.post(
        "/workflow/workspaces",
        json={
            "agent_id": "writer-api",
            "branch": "codex/api-workflow",
            "worktree": str(worktree),
        },
        headers=auth,
    )
    assert acquired.status_code == 200
    lease_id = acquired.json()["lease_id"]
    lease_auth = {
        **auth,
        "X-Workflow-Lease-Token": acquired.json()["lease_token"],
    }
    invalid_token = client.post(
        "/workflow/model-calls",
        json={"lease_id": lease_id},
        headers=auth,
    )
    assert invalid_token.status_code == 409
    renewed = client.post(f"/workflow/workspaces/{lease_id}/renew", headers=lease_auth)
    assert renewed.status_code == 200
    invalid_role = client.post(
        "/workflow/handoffs",
        json={
            "lease_id": lease_id,
            "source_role": "not-a-role",
            "target_role": "operator",
        },
        headers=lease_auth,
    )
    assert invalid_role.status_code == 422
    assert client.post(
        "/workflow/model-calls", json={"lease_id": lease_id}, headers=lease_auth
    ).json()["allowed"]
    commit_sha = _commit(worktree, "api.txt", "api\n")

    waiting = client.post(
        "/workflow/handoffs",
        json={
            "lease_id": lease_id,
            "source_role": "writer",
            "target_role": "operator",
            "commit_sha": commit_sha,
            "source_state": "ready",
            "approval_required": True,
        },
        headers=lease_auth,
    )
    assert waiting.status_code == 200
    handoff_id = waiting.json()["handoff_id"]
    pending_gate = client.post(
        "/workflow/model-calls",
        json={"lease_id": lease_id},
        headers=lease_auth,
    )
    assert pending_gate.status_code == 200
    assert pending_gate.json()["allowed"] is False
    assert "approval" in pending_gate.json()["required_action"].lower()
    duplicate_waiting = client.post(
        "/workflow/handoffs",
        json={
            "lease_id": lease_id,
            "source_role": "writer",
            "target_role": "operator",
            "commit_sha": commit_sha,
            "source_state": "duplicate",
        },
        headers=lease_auth,
    )
    assert duplicate_waiting.status_code == 409
    unresolved_release = client.post(
        f"/workflow/workspaces/{lease_id}/release", headers=lease_auth
    )
    assert unresolved_release.status_code == 409
    assert worktree.exists()
    assert (
        client.get(f"/workflow/handoffs/{handoff_id}", headers=auth).status_code == 200
    )
    clarified = client.post(
        f"/workflow/handoffs/{handoff_id}/clarify",
        json={"question": "Confirm the scope"},
        headers=lease_auth,
    )
    answered = client.post(
        f"/workflow/handoffs/{handoff_id}/clarify/answer",
        json={"answer": "The API only"},
        headers=lease_auth,
    )
    assert clarified.status_code == 200
    assert answered.status_code == 200
    blocked_gate = client.post(
        "/workflow/model-calls",
        json={"lease_id": lease_id},
        headers=lease_auth,
    )
    assert blocked_gate.status_code == 200
    assert blocked_gate.json()["allowed"] is False
    second_waiting = client.post(
        "/workflow/handoffs",
        json={
            "lease_id": lease_id,
            "source_role": "writer",
            "target_role": "operator",
            "commit_sha": commit_sha,
            "source_state": "ready again",
            "approval_required": True,
        },
        headers=lease_auth,
    )
    assert second_waiting.status_code == 200
    second_handoff_id = second_waiting.json()["handoff_id"]
    approved = client.post(
        f"/workflow/handoffs/{second_handoff_id}/approve",
        json={"actor": "writer", "note": "go"},
        headers=lease_auth,
    )
    assert approved.status_code == 200
    assert approved.json()["approval_actor"] == "operator"
    unknown = client.get("/workflow/handoffs/missing", headers=auth)
    assert unknown.status_code == 409

    release_worktree = tmp_path / "api-release"
    release = client.post(
        "/workflow/workspaces",
        json={
            "agent_id": "writer-release",
            "branch": "codex/api-release",
            "worktree": str(release_worktree),
        },
        headers=auth,
    )
    release_id = release.json()["lease_id"]
    release_auth = {
        **auth,
        "X-Workflow-Lease-Token": release.json()["lease_token"],
    }
    released = client.post(
        f"/workflow/workspaces/{release_id}/release", headers=release_auth
    )
    assert released.status_code == 200

    stop_worktree = tmp_path / "api-stop"
    stop = client.post(
        "/workflow/workspaces",
        json={
            "agent_id": "writer-stop",
            "branch": "codex/api-stop",
            "worktree": str(stop_worktree),
        },
        headers=auth,
    )
    stop_id = stop.json()["lease_id"]
    stop_auth = {
        **auth,
        "X-Workflow-Lease-Token": stop.json()["lease_token"],
    }
    stopped = client.post(
        f"/workflow/workspaces/{stop_id}/stop",
        json={"reason": "operator stop"},
        headers=stop_auth,
    )
    assert stopped.status_code == 200
    cleaned = client.post(f"/workflow/workspaces/{stop_id}/release", headers=stop_auth)
    assert cleaned.status_code == 200
    assert not stop_worktree.exists()
    missing_service = TestClient(create_app(api_key="test-key"))
    unavailable = missing_service.post(
        "/workflow/model-calls", json={"lease_id": "missing"}, headers=auth
    )
    assert unavailable.status_code == 503
    store.close()
