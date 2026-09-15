from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from beehaiive.models import (
    PbiSnapshot,
    ProjectSnapshot,
    RepositorySnapshot,
    RunState,
    RunStatus,
    Stage,
)
from beehaiive.orchestrator import Orchestrator
from beehaiive.storage import OrchestratorStore, StoreError
from tests.support.edges.helpers import storage_snapshot as _storage_snapshot
from tests.support.edges.storage_provider import StorageProvider as StorageProvider


def test_storage_external_sync_records_existing_run_id() -> None:
    store = OrchestratorStore()
    service = Orchestrator(store, StorageProvider(_storage_snapshot()))
    service.synchronize("project-1")
    run = service.claim("project-1", "owner/api", "worker-1")
    assert run is not None

    store.sync_project(
        ProjectSnapshot(
            "project-1",
            "Planning",
            (
                RepositorySnapshot(
                    "owner/api",
                    (
                        PbiSnapshot(
                            "owner/api", 1, "one", Stage.IMPLEMENT, "In Progress"
                        ),
                    ),
                ),
            ),
        )
    )

    pbi = store.project_state("project-1")["repositories"][0]["pbis"][0]  # type: ignore[index]
    assert pbi["events"][-1]["run_id"] == run.run_id  # type: ignore[index]
    store.close()


def test_storage_rejects_mismatched_pbi_repository() -> None:
    store = OrchestratorStore()
    with pytest.raises(StoreError, match="does not match"):
        store.sync_project(
            ProjectSnapshot(
                "project-1",
                "Planning",
                (
                    RepositorySnapshot(
                        "owner/api", (PbiSnapshot("owner/web", 1, "bad"),)
                    ),
                ),
            )
        )
    store.close()


def test_storage_defensive_handoff_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    store = OrchestratorStore()
    service = Orchestrator(store, StorageProvider(_storage_snapshot()))
    service.synchronize("project-1")
    run = service.claim("project-1", "owner/api", "worker-1")
    assert run is not None
    lease_token = run.lease_token or ""
    run = service.advance(run.run_id, Stage.IMPLEMENT, lease_token)

    orphan = RunState(
        "orphan",
        "missing-project",
        "owner/api",
        1,
        "orphan",
        Stage.IMPLEMENT,
        RunStatus.ACTIVE,
        1,
        owner_id="worker-1",
        lease_token=lease_token,
        lease_expires_at=run.lease_expires_at,
    )
    monkeypatch.setattr(store, "_run_for_id", lambda connection, run_id: orphan)
    with pytest.raises(StoreError, match="Unknown PBI"):
        store.prepare_handoff("orphan", "branch", "main", "body", lease_token)
    monkeypatch.undo()

    store.prepare_handoff(run.run_id, "branch", "main", "body", lease_token)
    pending_calls = 0

    def return_pending_once(connection: object, run_id: str) -> RunState | None:
        nonlocal pending_calls
        pending_calls += 1
        return run if pending_calls == 1 else None

    monkeypatch.setattr(store, "_run_for_id", return_pending_once)
    with pytest.raises(StoreError, match="Unknown run"):
        store.prepare_handoff(run.run_id, "branch", "main", "body", lease_token)
    store._connection.execute(
        """
        UPDATE pbis
        SET branch = NULL, handoff_base_branch = NULL, handoff_body = NULL,
            handoff_status = 'none'
        WHERE project_id = ? AND repository_name = ? AND number = ?
        """,
        (run.project_id, run.repository, run.pbi_number),
    )

    calls = 0

    def return_once(connection: object, run_id: str) -> RunState | None:
        nonlocal calls
        calls += 1
        return run if calls == 1 else None

    monkeypatch.setattr(store, "_run_for_id", return_once)
    with pytest.raises(StoreError, match="Unknown run"):
        store.prepare_handoff(run.run_id, "branch", "main", "body", lease_token)
    store.close()


def test_storage_migrates_legacy_columns(tmp_path: Path) -> None:
    database = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE projects(project_id TEXT PRIMARY KEY, name TEXT, updated_at TEXT);
        CREATE TABLE repositories(
            project_id TEXT, name TEXT, PRIMARY KEY(project_id, name)
        );
        CREATE TABLE pbis(
            project_id TEXT, repository_name TEXT, number INTEGER, title TEXT,
            stage TEXT, run_id TEXT, branch TEXT, pull_request_url TEXT,
            last_error TEXT, PRIMARY KEY(project_id, repository_name, number)
        );
        CREATE TABLE runs(
            run_id TEXT PRIMARY KEY, project_id TEXT, repository_name TEXT,
            pbi_number INTEGER, status TEXT, attempt INTEGER, last_error TEXT,
            updated_at TEXT
        );
        CREATE TABLE events(
            event_id INTEGER PRIMARY KEY, project_id TEXT, repository_name TEXT,
            pbi_number INTEGER, run_id TEXT, event_type TEXT, from_stage TEXT,
            to_stage TEXT, details_json TEXT, created_at TEXT
        );
        CREATE TABLE handoffs(
            run_id TEXT PRIMARY KEY, project_id TEXT, repository_name TEXT,
            pbi_number INTEGER, branch TEXT, pull_request_url TEXT,
            pull_request_number INTEGER, created_at TEXT
        );
        """
    )
    connection.close()

    store = OrchestratorStore(database)
    columns = {
        str(row[1]) for row in store._connection.execute("PRAGMA table_info(pbis)")
    }
    assert {
        "active",
        "handoff_base_branch",
        "handoff_body",
        "handoff_status",
        "planning_status",
        "claimable",
        "archived",
    } <= columns
    run_columns = {
        str(row[1]) for row in store._connection.execute("PRAGMA table_info(runs)")
    }
    assert {"owner_id", "lease_token", "lease_expires_at"} <= run_columns
    tables = {
        str(row[0])
        for row in store._connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert {
        "agent_sessions",
        "agent_session_events",
        "lifecycle_transition_evidence",
        "budget_decision_evidence",
    } <= tables
    assert {
        "canonical_state",
        "canonical_facts_json",
        "canonical_source_version",
    } <= columns
    budget_columns = {
        str(row[1])
        for row in store._connection.execute(
            "PRAGMA table_info(budget_decision_evidence)"
        )
    }
    assert {"snapshot_json", "fallback_model"} <= budget_columns
    store.close()
