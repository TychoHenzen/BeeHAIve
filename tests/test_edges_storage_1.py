from __future__ import annotations

import pytest

from beehaiive.models import (
    PbiSnapshot,
    ProjectSnapshot,
    RepositorySnapshot,
    RunStatus,
    Stage,
)
from beehaiive.orchestrator import Orchestrator
from beehaiive.storage import OrchestratorStore, StoreError
from tests.support.edges.helpers import storage_snapshot as _storage_snapshot
from tests.support.edges.storage_provider import StorageProvider as StorageProvider


def test_claim_recovery_cannot_claim_a_different_run() -> None:
    snapshot = ProjectSnapshot(
        "project-1",
        "Planning",
        (
            RepositorySnapshot(
                "owner/api",
                (
                    PbiSnapshot("owner/api", 1, "first"),
                    PbiSnapshot("owner/api", 2, "second"),
                ),
            ),
        ),
    )
    store = OrchestratorStore()
    store.sync_project(snapshot)
    previous = store.claim_next("project-1", "owner/api", "worker-1")
    assert previous is not None
    store._connection.execute(
        "UPDATE runs SET status = 'completed', owner_id = NULL, "
        "lease_token = NULL, lease_expires_at = NULL WHERE run_id = ?",
        (previous.run_id,),
    )
    store._connection.execute(
        "UPDATE pbis SET claimable = 0 WHERE project_id = ? "
        "AND repository_name = ? AND number = 1",
        ("project-1", "owner/api"),
    )

    assert (
        store.claim_next(
            "project-1",
            "owner/api",
            "recovery-worker",
            expected_run_id=previous.run_id,
        )
        is None
    )
    next_run = store.claim_next("project-1", "owner/api", "next-worker")
    assert next_run is not None and next_run.pbi_number == 2
    store.close()


def test_claim_can_target_the_selected_pbi() -> None:
    snapshot = ProjectSnapshot(
        "project-1",
        "Planning",
        (
            RepositorySnapshot(
                "owner/api",
                (
                    PbiSnapshot("owner/api", 1, "first"),
                    PbiSnapshot("owner/api", 2, "second"),
                ),
            ),
        ),
    )
    store = OrchestratorStore()
    store.sync_project(snapshot)

    selected = store.claim_next(
        "project-1", "owner/api", "worker-2", expected_pbi_number=2
    )

    assert selected is not None and selected.pbi_number == 2
    store.close()


def test_claim_renews_same_lease_and_registers_agent_session() -> None:
    store = OrchestratorStore()
    store.sync_project(_storage_snapshot())
    claimed = store.claim_next("project-1", "owner/api", "worker-1")
    assert claimed is not None and claimed.lease_token is not None

    renewed = store.claim_next(
        "project-1",
        "owner/api",
        "worker-1",
        claimed.lease_token,
        agent_session=("agent-worker", "persisted task"),
    )

    assert renewed is not None and renewed.run_id == claimed.run_id
    session = store.get_agent_session(claimed.run_id)
    assert session is not None and session["task"] == "persisted task"
    store.close()


def test_storage_rejects_invalid_state_operations() -> None:
    store = OrchestratorStore()
    service = Orchestrator(store, StorageProvider(_storage_snapshot()))
    service.synchronize("project-1")
    with pytest.raises(StoreError, match="worker owner"):
        store.claim_next("project-1", "owner/api", " ")
    with pytest.raises(StoreError, match="agent worker and task"):
        store.claim_next(
            "project-1", "owner/api", "worker-1", agent_session=("agent", " ")
        )
    run = service.claim("project-1", "owner/api", "worker-1")
    assert run is not None
    lease_token = run.lease_token or ""

    with pytest.raises(StoreError, match="Unknown run"):
        store.advance("missing", Stage.IMPLEMENT, lease_token)
    with pytest.raises(StoreError, match="Unknown run"):
        store.record_handoff("missing", "branch", "url", None, lease_token)
    with pytest.raises(StoreError, match="Unknown run"):
        store.prepare_handoff("missing", "branch", "main", "body", lease_token)
    with pytest.raises(StoreError, match="Unknown run"):
        store.fail("missing", "error", lease_token)
    with pytest.raises(StoreError, match="Unknown run"):
        store.pending_handoff("missing", lease_token)
    with pytest.raises(StoreError, match="Unknown run"):
        store.renew_lease("missing", lease_token)
    assert store.get_run("missing") is None

    renewed = service.renew_lease(run.run_id, lease_token)
    assert renewed.run_id == run.run_id
    store._connection.execute(
        "UPDATE runs SET lease_expires_at = ? WHERE run_id = ?",
        ("2000-01-01T00:00:00+00:00", run.run_id),
    )
    with pytest.raises(StoreError, match="expired"):
        store.advance(run.run_id, Stage.IMPLEMENT, lease_token)
    store._connection.execute(
        "UPDATE runs SET lease_expires_at = ? WHERE run_id = ?",
        (renewed.lease_expires_at, run.run_id),
    )

    advanced = store.advance(run.run_id, Stage.REFINE, lease_token)
    assert advanced.stage is Stage.REFINE
    with pytest.raises(StoreError, match="Cannot advance"):
        store.advance(run.run_id, Stage.PULL_REQUEST, lease_token)
    with pytest.raises(StoreError, match="failure reason"):
        store.fail(run.run_id, "", lease_token)
    failed = store.fail(run.run_id, "failed", lease_token)
    assert failed.status is RunStatus.FAILED
    assert store.fail(run.run_id, "again", lease_token) == failed
    assert store.advance(run.run_id, Stage.REFINE, lease_token) == failed
    with pytest.raises(StoreError, match="not active"):
        store.advance(run.run_id, Stage.IMPLEMENT, lease_token)
    with pytest.raises(StoreError, match="branch and pull-request"):
        store.record_handoff(run.run_id, "", "url", None, lease_token)
    with pytest.raises(StoreError, match="active implementation"):
        store.record_handoff(run.run_id, "branch", "url", None, lease_token)
    with pytest.raises(StoreError, match="active implementation"):
        store.prepare_handoff(run.run_id, "branch", "main", "body", lease_token)
    store.close()


def test_storage_handoff_intent_edge_cases() -> None:
    store = OrchestratorStore()
    service = Orchestrator(store, StorageProvider(_storage_snapshot()))
    service.synchronize("project-1")
    run = service.claim("project-1", "owner/api", "worker-1")
    assert run is not None
    lease_token = run.lease_token or ""
    run = service.advance(run.run_id, Stage.IMPLEMENT, lease_token)

    with pytest.raises(StoreError, match="branch is required"):
        store.prepare_handoff(run.run_id, "", "main", "body", lease_token)
    with pytest.raises(StoreError, match="persisted intent"):
        store.record_handoff(run.run_id, "branch", "url", None, lease_token)
    intent = store.prepare_handoff(run.run_id, "branch", "main", "body", lease_token)
    with pytest.raises(StoreError, match="persisted intent"):
        store.prepare_handoff(run.run_id, "other", "main", "body", lease_token)
    completed = store.record_handoff(run.run_id, "branch", "url", 1, lease_token)
    assert (
        store.record_handoff(run.run_id, "branch", "url", 1, lease_token) == completed
    )
    finished_intent = store.prepare_handoff(
        run.run_id, "branch", "main", "body", lease_token
    )
    assert finished_intent.run.status is RunStatus.COMPLETED
    assert intent.branch == finished_intent.branch
    with pytest.raises(StoreError, match="completed"):
        store.fail(run.run_id, "late failure", lease_token)
    store._connection.execute(
        """
        UPDATE pbis SET stage = 'implement', handoff_status = 'completed'
        WHERE project_id = ? AND repository_name = ? AND number = ?
        """,
        (run.project_id, run.repository, run.pbi_number),
    )
    store._connection.execute(
        "UPDATE runs SET status = 'active' WHERE run_id = ?", (run.run_id,)
    )
    with pytest.raises(StoreError, match="already completed"):
        store.prepare_handoff(run.run_id, "branch", "main", "body", lease_token)
    store.close()


def test_storage_reconciles_an_active_removed_pbi() -> None:
    store = OrchestratorStore()
    service = Orchestrator(store, StorageProvider(_storage_snapshot()))
    service.synchronize("project-1")
    run = service.claim("project-1", "owner/api", "worker-1")
    assert run is not None
    store.sync_project(ProjectSnapshot("project-1", "Planning", ()))
    assert store.get_run(run.run_id) is not None
    assert store.get_run(run.run_id).status is RunStatus.FAILED  # type: ignore[union-attr]
    store.close()
