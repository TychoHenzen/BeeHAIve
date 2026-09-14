from __future__ import annotations

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from typing import Any

from .helpers import finding_fingerprint
from .review_concern import ReviewConcern


class ReviewStoreCoreMixin:
    def __init__(self: Any, database: str | Path = ":memory:") -> None:
        if database != ":memory:":
            Path(database).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            str(database), check_same_thread=False, isolation_level=None
        )
        self._connection.row_factory = sqlite3.Row
        self._lock = RLock()
        self._initialize()

    def close(self: Any) -> None:
        with self._lock:
            self._connection.close()

    @contextmanager
    def transaction(self: Any) -> Generator[sqlite3.Connection]:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
            except Exception:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()

    def _initialize(self: Any) -> None:
        with self._lock:
            self._connection.executescript(
                """
                    PRAGMA foreign_keys = ON;
                    CREATE TABLE IF NOT EXISTS review_cycles (
                        cycle_id TEXT PRIMARY KEY,
                        pull_request_id TEXT NOT NULL,
                        head_sha TEXT NOT NULL,
                        cycle_number INTEGER NOT NULL,
                        status TEXT NOT NULL,
                        human_approval INTEGER NOT NULL DEFAULT 0,
                        required_action TEXT,
                        approval_actor TEXT,
                        approval_reason TEXT,
                        approval_at TEXT,
                        github_evidence_json TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(pull_request_id, cycle_number)
                    );
                    CREATE TABLE IF NOT EXISTS review_readers (
                        cycle_id TEXT NOT NULL,
                        concern TEXT NOT NULL,
                        status TEXT NOT NULL,
                        finding_ids_json TEXT NOT NULL,
                        reader TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        claim_token TEXT,
                        claim_expires_at TEXT,
                        evidence_refs_json TEXT NOT NULL DEFAULT '[]',
                        PRIMARY KEY(cycle_id, concern),
                        FOREIGN KEY(cycle_id) REFERENCES review_cycles(cycle_id)
                    );
                    CREATE TABLE IF NOT EXISTS review_findings (
                        finding_id TEXT PRIMARY KEY,
                        pull_request_id TEXT NOT NULL,
                        cycle_id TEXT NOT NULL,
                        concern TEXT NOT NULL,
                        summary TEXT NOT NULL,
                        status TEXT NOT NULL,
                        resolution TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        evidence_refs_json TEXT NOT NULL DEFAULT '[]',
                        head_sha TEXT,
                        fingerprint TEXT,
                        file_path TEXT,
                        start_line INTEGER,
                        end_line INTEGER,
                        duplicate_target TEXT,
                        first_seen_cycle_id TEXT,
                        stale INTEGER NOT NULL DEFAULT 0,
                        resolution_actor TEXT,
                        resolution_at TEXT,
                        publication_state TEXT NOT NULL DEFAULT 'unpublished',
                        publication_channel TEXT,
                        remote_id TEXT,
                        remote_url TEXT,
                        publication_attempts INTEGER NOT NULL DEFAULT 0,
                        publication_retry_evidence_json TEXT,
                        publication_claim_token TEXT,
                        publication_claim_expires_at TEXT,
                        FOREIGN KEY(cycle_id) REFERENCES review_cycles(cycle_id)
                    );
                    CREATE TABLE IF NOT EXISTS review_repair_attempts (
                        attempt_id TEXT PRIMARY KEY,
                        cycle_id TEXT NOT NULL UNIQUE,
                        pull_request_id TEXT NOT NULL,
                        head_sha TEXT NOT NULL,
                        finding_ids_json TEXT NOT NULL,
                        actor TEXT NOT NULL,
                        status TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        lease_id TEXT,
                        commit_sha TEXT,
                        push_evidence_json TEXT,
                        result TEXT,
                        required_action TEXT,
                        cancellation_requested INTEGER NOT NULL DEFAULT 0,
                        review_transition_status TEXT NOT NULL DEFAULT 'not_required',
                        review_transition_cycle_id TEXT,
                        review_transition_required_action TEXT,
                        FOREIGN KEY(cycle_id) REFERENCES review_cycles(cycle_id)
                    );
                    CREATE INDEX IF NOT EXISTS review_cycles_by_pull_request
                        ON review_cycles(pull_request_id, cycle_number DESC);
                    CREATE INDEX IF NOT EXISTS review_findings_by_pull_request
                        ON review_findings(pull_request_id, created_at);
                    """
            )
            cycle_columns = {
                str(row["name"])
                for row in self._connection.execute(
                    "PRAGMA table_info(review_cycles)"
                ).fetchall()
            }
            for column in (
                "approval_actor",
                "approval_reason",
                "approval_at",
                "github_evidence_json",
            ):
                if column not in cycle_columns:
                    self._connection.execute(
                        f"ALTER TABLE review_cycles ADD COLUMN {column} TEXT"
                    )
            repair_columns = {
                str(row["name"])
                for row in self._connection.execute(
                    "PRAGMA table_info(review_repair_attempts)"
                ).fetchall()
            }
            for column, definition in (
                (
                    "review_transition_status",
                    "TEXT NOT NULL DEFAULT 'not_required'",
                ),
                ("review_transition_cycle_id", "TEXT"),
                ("review_transition_required_action", "TEXT"),
            ):
                if column not in repair_columns:
                    self._connection.execute(
                        f"ALTER TABLE review_repair_attempts ADD COLUMN "
                        f"{column} {definition}"
                    )
            reader_columns = {
                str(row["name"])
                for row in self._connection.execute(
                    "PRAGMA table_info(review_readers)"
                ).fetchall()
            }
            for column in ("claim_token", "claim_expires_at"):
                if column not in reader_columns:
                    self._connection.execute(
                        f"ALTER TABLE review_readers ADD COLUMN {column} TEXT"
                    )
            if "evidence_refs_json" not in reader_columns:
                self._connection.execute(
                    "ALTER TABLE review_readers ADD COLUMN "
                    "evidence_refs_json TEXT NOT NULL DEFAULT '[]'"
                )
            finding_columns = {
                str(row["name"])
                for row in self._connection.execute(
                    "PRAGMA table_info(review_findings)"
                ).fetchall()
            }
            if "evidence_refs_json" not in finding_columns:
                self._connection.execute(
                    "ALTER TABLE review_findings ADD COLUMN "
                    "evidence_refs_json TEXT NOT NULL DEFAULT '[]'"
                )
            new_finding_columns = (
                ("head_sha", "TEXT"),
                ("fingerprint", "TEXT"),
                ("file_path", "TEXT"),
                ("start_line", "INTEGER"),
                ("end_line", "INTEGER"),
                ("duplicate_target", "TEXT"),
                ("first_seen_cycle_id", "TEXT"),
                ("stale", "INTEGER NOT NULL DEFAULT 0"),
                ("resolution_actor", "TEXT"),
                ("resolution_at", "TEXT"),
                (
                    "publication_state",
                    "TEXT NOT NULL DEFAULT 'unpublished'",
                ),
                ("publication_channel", "TEXT"),
                ("remote_id", "TEXT"),
                ("remote_url", "TEXT"),
                ("publication_attempts", "INTEGER NOT NULL DEFAULT 0"),
                ("publication_retry_evidence_json", "TEXT"),
                ("publication_claim_token", "TEXT"),
                ("publication_claim_expires_at", "TEXT"),
            )
            for column, definition in new_finding_columns:
                if column not in finding_columns:
                    self._connection.execute(
                        f"ALTER TABLE review_findings ADD COLUMN {column} {definition}"
                    )
            rows = self._connection.execute(
                """
                    SELECT finding.finding_id, finding.cycle_id, finding.concern,
                           finding.summary, finding.file_path, finding.start_line,
                           finding.end_line, finding.head_sha, finding.fingerprint,
                           finding.first_seen_cycle_id, cycle.head_sha AS cycle_head_sha
                    FROM review_findings AS finding
                    JOIN review_cycles AS cycle ON cycle.cycle_id = finding.cycle_id
                    WHERE finding.head_sha IS NULL OR finding.fingerprint IS NULL
                       OR finding.first_seen_cycle_id IS NULL
                    """
            ).fetchall()
            for row in rows:
                concern = ReviewConcern(str(row["concern"]))
                fingerprint = row["fingerprint"] or finding_fingerprint(
                    concern,
                    str(row["summary"]),
                    row["file_path"],
                    row["start_line"],
                    row["end_line"],
                )
                self._connection.execute(
                    """
                        UPDATE review_findings
                        SET head_sha = COALESCE(head_sha, ?),
                            fingerprint = COALESCE(fingerprint, ?),
                            first_seen_cycle_id = COALESCE(first_seen_cycle_id, ?)
                        WHERE finding_id = ?
                        """,
                    (
                        row["cycle_head_sha"],
                        fingerprint,
                        row["cycle_id"],
                        row["finding_id"],
                    ),
                )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS review_findings_by_identity "
                "ON review_findings(pull_request_id, head_sha, fingerprint)"
            )
