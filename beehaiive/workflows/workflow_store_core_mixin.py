from __future__ import annotations

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

from beehaiive.sqlite_store_core import SQLiteStoreCoreMixin

from .constants import DEFAULT_LEASE_TTL_SECONDS
from .helpers import current_timestamp, lease_expiry
from .lease_status import LeaseStatus
from .repair_status import RepairStatus
from .workflow_error import WorkflowError


class WorkflowStoreCoreMixin(SQLiteStoreCoreMixin):
    def __init__(
        self: Any,
        database: str | Path = ":memory:",
        lease_ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
    ) -> None:
        if lease_ttl_seconds <= 0:
            raise WorkflowError("Lease TTL must be positive")
        self._lease_ttl_seconds = lease_ttl_seconds
        SQLiteStoreCoreMixin.__init__(self, database, self._initialize)

    @property
    def lease_heartbeat_seconds(self: Any) -> float:
        return max(self._lease_ttl_seconds / 3, 0.01)

    @contextmanager
    def _transaction(
        self: Any, *, reclaim_expired: bool = True
    ) -> Generator[sqlite3.Connection]:
        before_yield = self._expire_active_leases if reclaim_expired else None
        with self._sqlite_transaction(before_yield=before_yield) as connection:
            yield connection

    def _initialize(self: Any) -> None:
        with self._lock:
            self._connection.executescript(
                """
                    PRAGMA foreign_keys = ON;
                    CREATE TABLE IF NOT EXISTS workflow_leases (
                        lease_id TEXT PRIMARY KEY,
                        agent_id TEXT NOT NULL,
                        branch TEXT NOT NULL,
                        worktree_path TEXT NOT NULL,
                        lease_token TEXT,
                        expires_at TEXT,
                        status TEXT NOT NULL,
                        stop_reason TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    DROP INDEX IF EXISTS active_workflow_branch;
                    DROP INDEX IF EXISTS active_workflow_worktree;
                    CREATE UNIQUE INDEX active_workflow_branch
                        ON workflow_leases(branch)
                        WHERE status IN ('active', 'retained');
                    CREATE UNIQUE INDEX active_workflow_worktree
                        ON workflow_leases(worktree_path)
                        WHERE status IN ('active', 'retained');
                    CREATE UNIQUE INDEX IF NOT EXISTS active_dashboard_run_workspace
                        ON workflow_leases(agent_id)
                        WHERE agent_id GLOB 'dashboard-run:*'
                        AND status IN ('active', 'retained');
                    CREATE TABLE IF NOT EXISTS workflow_handoffs (
                        handoff_id TEXT PRIMARY KEY,
                        lease_id TEXT NOT NULL,
                        source_role TEXT NOT NULL,
                        target_role TEXT NOT NULL,
                        commit_sha TEXT NOT NULL,
                        source_state TEXT NOT NULL,
                        status TEXT NOT NULL,
                        checks_json TEXT NOT NULL,
                        constitution_json TEXT NOT NULL,
                        required_action TEXT,
                        approval_actor TEXT,
                        approval_note TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        FOREIGN KEY(lease_id) REFERENCES workflow_leases(lease_id)
                    );
                    CREATE INDEX IF NOT EXISTS workflow_handoffs_by_lease
                        ON workflow_handoffs(lease_id, created_at DESC);
                    CREATE TABLE IF NOT EXISTS workflow_gates (
                        gate_id TEXT PRIMARY KEY,
                        lease_id TEXT NOT NULL,
                        gate TEXT NOT NULL,
                        allowed INTEGER NOT NULL,
                        checks_json TEXT NOT NULL,
                        required_action TEXT,
                        created_at TEXT NOT NULL,
                        FOREIGN KEY(lease_id) REFERENCES workflow_leases(lease_id)
                    );
                    CREATE TABLE IF NOT EXISTS workflow_repairs (
                        repair_id TEXT PRIMARY KEY,
                        pull_request_id TEXT NOT NULL,
                        repository TEXT NOT NULL,
                        pull_request_number INTEGER NOT NULL,
                        source_branch TEXT NOT NULL,
                        target_branch TEXT NOT NULL,
                        expected_head TEXT NOT NULL,
                        target_head TEXT NOT NULL,
                        repair_branch TEXT NOT NULL,
                        worktree_path TEXT NOT NULL,
                        lease_id TEXT,
                        status TEXT NOT NULL,
                        repaired_head TEXT,
                        checks_json TEXT NOT NULL,
                        evidence_json TEXT NOT NULL,
                        required_action TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE UNIQUE INDEX IF NOT EXISTS workflow_repairs_identity
                        ON workflow_repairs(pull_request_id, expected_head);
                    """
            )
            lease_columns = {
                str(row["name"])
                for row in self._connection.execute(
                    "PRAGMA table_info(workflow_leases)"
                ).fetchall()
            }
            for column in ("lease_token", "expires_at"):
                if column not in lease_columns:
                    self._connection.execute(
                        f"ALTER TABLE workflow_leases ADD COLUMN {column} TEXT"
                    )
            active_rows = self._connection.execute(
                """
                    SELECT lease_id, lease_token, expires_at
                    FROM workflow_leases
                    WHERE status = ?
                    """,
                (LeaseStatus.ACTIVE.value,),
            ).fetchall()
            for row in active_rows:
                if row["lease_token"] is None or row["expires_at"] is None:
                    self._connection.execute(
                        """
                            UPDATE workflow_leases
                            SET lease_token = ?, expires_at = ?
                            WHERE lease_id = ?
                            """,
                        (
                            str(uuid4()),
                            lease_expiry(self._lease_ttl_seconds),
                            str(row["lease_id"]),
                        ),
                    )

            self._connection.execute(
                """
                    UPDATE workflow_repairs
                    SET status = ?, required_action = ?, updated_at = ?
                    WHERE lease_id IN (
                        SELECT lease_id FROM workflow_leases WHERE status = ?
                    ) AND status = ?
                    """,
                (
                    RepairStatus.AWAITING_CLARIFICATION.value,
                    "Lease expired and requires operator recovery",
                    current_timestamp(),
                    LeaseStatus.STOPPED.value,
                    RepairStatus.RUNNING.value,
                ),
            )
