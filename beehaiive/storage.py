"""SQLite-backed state for durable project orchestration."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import RLock
from typing import cast
from uuid import uuid4

from .contracts import ContractError, TaskContract, TaskOutcome, TaskResult
from .models import (
    ARCHIVE_PROJECT_STATUS,
    PROJECT_TERMINAL_STATUSES,
    HandoffIntent,
    PbiSnapshot,
    ProjectSnapshot,
    RoutingFailure,
    RunState,
    RunStatus,
    Stage,
)


class StoreError(RuntimeError):
    """Raised when persisted orchestration state cannot satisfy an operation."""


MAX_AGENT_SESSION_EVENTS = 100
MAX_AGENT_SESSION_EVENT_LENGTH = 4_000
MAX_AGENT_SESSION_TEXT_BYTES = 64_000


def _task_claimability_state(
    contract_json: object,
    result_json: object,
    answer: object,
    answer_resumed: object,
) -> tuple[bool, bool]:
    if contract_json is None:
        return result_json is not None, False
    if not isinstance(contract_json, str) or not contract_json:
        return True, False
    if not isinstance(result_json, str) or not result_json:
        return True, False
    try:
        contract = TaskContract.from_dict(json.loads(contract_json))
        result = TaskResult.from_payload(
            json.loads(result_json), contract, allow_answer=True
        )
    except (ValueError, TypeError, RecursionError):
        return True, False
    answered_question = (
        result.outcome is TaskOutcome.QUESTION
        and result.answer is not None
        and result.answer == answer
        and bool(answer_resumed)
    )
    paused = result.outcome is TaskOutcome.BLOCKED or (
        result.outcome is TaskOutcome.QUESTION and not answered_question
    )
    return paused, answered_question


def _task_claimability_for_run(
    connection: sqlite3.Connection, run_id: str
) -> tuple[bool, bool]:
    row = connection.execute(
        """
        SELECT task_contract_json, task_result_json, task_answer,
               task_answer_resumed
        FROM runs WHERE run_id = ?
        """,
        (run_id,),
    ).fetchone()
    if row is None:
        return True, False
    return _task_claimability_state(
        row["task_contract_json"],
        row["task_result_json"],
        row["task_answer"],
        row["task_answer_resumed"],
    )


_STAGE_ORDER = {
    Stage.BACKLOG: 0,
    Stage.REFINE: 1,
    Stage.IMPLEMENT: 2,
    Stage.PULL_REQUEST: 3,
}
DEFAULT_EVENT_LIMIT = 100
MAX_EVENT_LIMIT = 500
DEFAULT_ACTION_LIMIT = 50
MAX_ACTION_LIMIT = 200
MAX_AGENT_RESULT_LENGTH = 4_000
MAX_META_REVIEW_RECORDS = 25
MAX_META_REVIEW_INPUT_TOKENS = 8_000
MAX_META_REVIEW_SUGGESTIONS = 25
MAX_META_REVIEW_TEXT_LENGTH = 500
MAX_META_REVIEW_ATTEMPTS = 20
MAX_META_REVIEW_EVENT_DETAILS_LENGTH = 8_000
META_REVIEW_LEASE_SECONDS = 900


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _lease_is_active(expires_at: str | None) -> bool:
    if not expires_at:
        return False
    try:
        return datetime.fromisoformat(expires_at) > datetime.now(UTC)
    except ValueError:
        return False


def _json_mapping(value: object) -> dict[str, object]:
    if not isinstance(value, str) or not value:
        return {}
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return cast(dict[str, object], decoded) if isinstance(decoded, dict) else {}


def _json_mapping_or_none(value: object) -> dict[str, object] | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return None
    return cast(dict[str, object], decoded) if isinstance(decoded, dict) else None


def _archive_eligible(pbi: PbiSnapshot, run_status: str | None = None) -> bool:
    if run_status in {RunStatus.ACTIVE.value, RunStatus.FAILED.value}:
        return False
    if (pbi.planning_status or "").strip().lower() not in PROJECT_TERMINAL_STATUSES:
        return False
    if (pbi.planning_status or "").strip().lower() != ARCHIVE_PROJECT_STATUS:
        return False
    pull_requests = pbi.metadata.get("pull_requests")
    if not isinstance(pull_requests, Sequence) or isinstance(
        pull_requests, (str, bytes, bytearray)
    ):
        return False
    return any(
        isinstance(raw_pull_request, Mapping)
        and (pull_request := cast(Mapping[str, object], raw_pull_request)).get("merged")
        is True
        and pull_request.get("source_branch_state") == "deleted"
        for raw_pull_request in cast(Sequence[object], pull_requests)
    )


def _bounded_event_details(value: object) -> dict[str, object]:
    if not isinstance(value, str) or len(value) > MAX_META_REVIEW_EVENT_DETAILS_LENGTH:
        return {}
    return _json_mapping(value)


def _json_list(value: object) -> list[object]:
    if not isinstance(value, str) or not value:
        return []
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return []
    return cast(list[object], decoded) if isinstance(decoded, list) else []


class OrchestratorStore:
    """Thread-safe SQLite store with one active writer index per repository."""

    def __init__(
        self, database: str | Path = ":memory:", lease_seconds: int = 300
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        self._lease_seconds = lease_seconds
        if database != ":memory:":
            Path(database).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            str(database),
            check_same_thread=False,
            isolation_level=None,
        )
        self._connection.row_factory = sqlite3.Row
        self._lock = RLock()
        self._initialize()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _initialize(self) -> None:
        with self._lock:
            self._connection.executescript(
                """
                PRAGMA foreign_keys = ON;

                CREATE TABLE IF NOT EXISTS projects (
                    project_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS repositories (
                    project_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY (project_id, name),
                    FOREIGN KEY (project_id)
                        REFERENCES projects(project_id)
                        ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS pbis (
                    project_id TEXT NOT NULL,
                    repository_name TEXT NOT NULL,
                    number INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    archived INTEGER NOT NULL DEFAULT 0,
                    run_id TEXT,
                    branch TEXT,
                    pull_request_url TEXT,
                    last_error TEXT,
                    handoff_base_branch TEXT,
                    handoff_body TEXT,
                    handoff_head_sha TEXT,
                    handoff_verification_evidence TEXT,
                    handoff_status TEXT,
                    planning_status TEXT,
                    claimable INTEGER NOT NULL DEFAULT 1,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY (project_id, repository_name, number),
                    FOREIGN KEY (project_id, repository_name)
                        REFERENCES repositories(project_id, name) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    repository_name TEXT NOT NULL,
                    pbi_number INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    owner_id TEXT,
                    lease_token TEXT,
                    lease_expires_at TEXT,
                    execution_token TEXT,
                    last_error TEXT,
                    last_result TEXT,
                    task_contract_json TEXT,
                    task_result_json TEXT,
                    task_answer TEXT,
                    task_answer_resumed INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    UNIQUE (project_id, repository_name, pbi_number),
                    FOREIGN KEY (project_id, repository_name, pbi_number)
                        REFERENCES pbis(project_id, repository_name, number)
                        ON DELETE CASCADE
                );

                CREATE UNIQUE INDEX IF NOT EXISTS active_writer_per_repository
                    ON runs(project_id, repository_name)
                    WHERE status = 'active';

                CREATE TABLE IF NOT EXISTS events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id TEXT NOT NULL,
                    repository_name TEXT NOT NULL,
                    pbi_number INTEGER NOT NULL,
                    run_id TEXT,
                    event_type TEXT NOT NULL,
                    from_stage TEXT,
                    to_stage TEXT,
                    details_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (project_id, repository_name, pbi_number)
                        REFERENCES pbis(project_id, repository_name, number)
                        ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS agent_sessions (
                    run_id TEXT PRIMARY KEY,
                    worker_id TEXT NOT NULL,
                    task TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS agent_session_events (
                    run_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    role TEXT,
                    text TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    PRIMARY KEY (run_id, sequence),
                    FOREIGN KEY (run_id)
                        REFERENCES agent_sessions(run_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS handoffs (
                    run_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    repository_name TEXT NOT NULL,
                    pbi_number INTEGER NOT NULL,
                    branch TEXT NOT NULL,
                    pull_request_url TEXT NOT NULL,
                    pull_request_number INTEGER,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS actions (
                    action_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    repository_name TEXT,
                    pbi_number INTEGER,
                    run_id TEXT,
                    request_json TEXT NOT NULL,
                    result_json TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS routing_failure_outbox (
                    transition_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    error TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL,
                    output_tokens INTEGER NOT NULL,
                    recursive_spawn_depth INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    processed_at TEXT,
                    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS routing_failure_outbox_pending
                    ON routing_failure_outbox(run_id, status, created_at);

                CREATE INDEX IF NOT EXISTS actions_by_project
                    ON actions(project_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS meta_review_runs (
                    review_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    record_limit INTEGER NOT NULL,
                    input_token_limit INTEGER NOT NULL,
                    selected_records INTEGER NOT NULL DEFAULT 0,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    missing_evidence_json TEXT NOT NULL DEFAULT '[]',
                    error TEXT,
                    created_at TEXT NOT NULL,
                    completed_at TEXT,
                    FOREIGN KEY (project_id) REFERENCES projects(project_id)
                        ON DELETE CASCADE
                );

                CREATE UNIQUE INDEX IF NOT EXISTS active_meta_review_project
                    ON meta_review_runs(project_id)
                    WHERE status = 'running';

                CREATE UNIQUE INDEX IF NOT EXISTS active_meta_review_global
                    ON meta_review_runs(status)
                    WHERE status = 'running';

                CREATE TABLE IF NOT EXISTS meta_review_suggestions (
                    suggestion_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    suggestion_key TEXT NOT NULL,
                    proposed_outcome TEXT NOT NULL,
                    rationale TEXT NOT NULL,
                    evidence_refs_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_review_id TEXT NOT NULL,
                    UNIQUE (project_id, suggestion_key),
                    FOREIGN KEY (project_id) REFERENCES projects(project_id)
                        ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS meta_review_suggestions_by_project
                    ON meta_review_suggestions(project_id, status, created_at);
                """
            )
            repository_columns = {
                str(row["name"])
                for row in self._connection.execute(
                    "PRAGMA table_info(repositories)"
                ).fetchall()
            }
            if "active" not in repository_columns:
                self._connection.execute(
                    """
                    ALTER TABLE repositories
                    ADD COLUMN active INTEGER NOT NULL DEFAULT 1
                    """
                )
            pbi_columns = {
                str(row["name"])
                for row in self._connection.execute(
                    "PRAGMA table_info(pbis)"
                ).fetchall()
            }
            if "active" not in pbi_columns:
                self._connection.execute(
                    "ALTER TABLE pbis ADD COLUMN active INTEGER NOT NULL DEFAULT 1"
                )
            run_columns = {
                str(row["name"])
                for row in self._connection.execute(
                    "PRAGMA table_info(runs)"
                ).fetchall()
            }
            for column in (
                "owner_id",
                "lease_token",
                "lease_expires_at",
                "execution_token",
                "last_result",
                "task_contract_json",
                "task_result_json",
                "task_answer",
            ):
                if column not in run_columns:
                    self._connection.execute(
                        f"ALTER TABLE runs ADD COLUMN {column} TEXT"
                    )
            if "task_answer_resumed" not in run_columns:
                self._connection.execute(
                    "ALTER TABLE runs ADD COLUMN task_answer_resumed "
                    "INTEGER NOT NULL DEFAULT 0"
                )
            for column in (
                "handoff_base_branch",
                "handoff_body",
                "handoff_head_sha",
                "handoff_verification_evidence",
                "handoff_status",
                "planning_status",
                "claimable",
                "metadata_json",
                "archived",
            ):
                if column not in pbi_columns:
                    definition = (
                        "INTEGER NOT NULL DEFAULT 1"
                        if column == "claimable"
                        else "INTEGER NOT NULL DEFAULT 0"
                        if column == "archived"
                        else "TEXT NOT NULL DEFAULT '{}'"
                        if column == "metadata_json"
                        else "TEXT"
                    )
                    self._connection.execute(
                        f"ALTER TABLE pbis ADD COLUMN {column} {definition}"
                    )

    @contextmanager
    def _transaction(self) -> Generator[sqlite3.Connection]:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
            except Exception:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()

    def _lease_deadline(self) -> str:
        return (datetime.now(UTC) + timedelta(seconds=self._lease_seconds)).isoformat()

    @staticmethod
    def _require_lease(run: RunState, lease_token: str) -> None:
        if not lease_token or run.lease_token != lease_token:
            raise StoreError("Invalid or missing run lease token")
        if not _lease_is_active(run.lease_expires_at):
            raise StoreError(f"Run {run.run_id} lease has expired")

    def _renew_lease(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        lease_token: str,
    ) -> None:
        connection.execute(
            """
            UPDATE runs
            SET lease_expires_at = ?, updated_at = ?
            WHERE run_id = ? AND status = 'active' AND lease_token = ?
            """,
            (self._lease_deadline(), _now(), run_id, lease_token),
        )

    def sync_project(self, snapshot: ProjectSnapshot) -> tuple[str, ...]:
        removed_run_ids: list[str] = []
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO projects(project_id, name, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(project_id) DO UPDATE SET
                    name = excluded.name,
                    updated_at = excluded.updated_at
                """,
                (snapshot.project_id, snapshot.name, _now()),
            )
            connection.execute(
                "UPDATE repositories SET active = 0 WHERE project_id = ?",
                (snapshot.project_id,),
            )
            connection.execute(
                "UPDATE pbis SET active = 0, archived = 0 WHERE project_id = ?",
                (snapshot.project_id,),
            )
            for repository in snapshot.repositories:
                connection.execute(
                    """
                    INSERT INTO repositories(project_id, name, active)
                    VALUES (?, ?, 1)
                    ON CONFLICT(project_id, name) DO UPDATE SET active = 1
                    """,
                    (snapshot.project_id, repository.name),
                )
                for pbi in repository.pbis:
                    if pbi.repository != repository.name:
                        raise StoreError(
                            "PBI repository does not match its repository snapshot"
                        )
                    incoming_stage = (
                        pbi.stage
                        if pbi.claimable
                        and pbi.stage
                        in {
                            Stage.BACKLOG,
                            Stage.REFINE,
                            Stage.IMPLEMENT,
                        }
                        else None
                    )
                    existing = connection.execute(
                        """
                        SELECT p.stage, p.run_id, p.claimable,
                               r.status AS run_status, r.task_contract_json,
                               r.task_result_json, r.task_answer,
                               r.task_answer_resumed
                        FROM pbis AS p
                        LEFT JOIN runs AS r
                          ON r.project_id = p.project_id
                         AND r.repository_name = p.repository_name
                         AND r.pbi_number = p.number
                        WHERE p.project_id = ? AND p.repository_name = ?
                          AND p.number = ?
                        """,
                        (snapshot.project_id, pbi.repository, pbi.number),
                    ).fetchone()
                    if existing is None:
                        connection.execute(
                            """
                            INSERT INTO pbis(
                                project_id, repository_name, number, title,
                                stage, active, archived, planning_status, claimable,
                                metadata_json
                            )
                            VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                            """,
                            (
                                snapshot.project_id,
                                pbi.repository,
                                pbi.number,
                                pbi.title,
                                (incoming_stage or Stage.BACKLOG).value,
                                int(_archive_eligible(pbi)),
                                pbi.planning_status,
                                int(incoming_stage is not None),
                                json.dumps(dict(pbi.metadata), sort_keys=True),
                            ),
                        )
                        continue

                    current_stage = Stage(str(existing["stage"]))
                    task_pause, _ = _task_claimability_state(
                        existing["task_contract_json"],
                        existing["task_result_json"],
                        existing["task_answer"],
                        existing["task_answer_resumed"],
                    )
                    merged_stage = (
                        max(
                            (current_stage, incoming_stage),
                            key=lambda stage: _STAGE_ORDER[stage],
                        )
                        if incoming_stage is not None
                        else current_stage
                    )
                    connection.execute(
                        """
                        UPDATE pbis
                        SET title = ?, stage = ?, active = 1, archived = ?,
                            metadata_json = ?,
                            planning_status = ?, claimable = ?,
                            last_error = CASE
                                WHEN ? = ? THEN last_error
                                ELSE NULL
                            END
                        WHERE project_id = ? AND repository_name = ? AND number = ?
                        """,
                        (
                            pbi.title,
                            merged_stage.value,
                            int(
                                _archive_eligible(
                                    pbi,
                                    str(existing["run_status"])
                                    if isinstance(existing["run_status"], str)
                                    else None,
                                )
                            ),
                            json.dumps(dict(pbi.metadata), sort_keys=True),
                            pbi.planning_status,
                            int(
                                incoming_stage is not None
                                and merged_stage is not Stage.PULL_REQUEST
                                and existing["run_status"] != RunStatus.COMPLETED.value
                                and not task_pause
                            ),
                            current_stage.value,
                            merged_stage.value,
                            snapshot.project_id,
                            pbi.repository,
                            pbi.number,
                        ),
                    )
                    if merged_stage is not current_stage:
                        run_id = existing["run_id"]
                        if run_id is not None:
                            run_id = str(run_id)
                        self._record_event(
                            connection,
                            snapshot.project_id,
                            pbi.repository,
                            pbi.number,
                            run_id,
                            "external_sync",
                            current_stage,
                            merged_stage,
                            {
                                "source_stage": pbi.planning_status
                                or (
                                    pbi.stage.value
                                    if pbi.stage is not None
                                    else "unknown"
                                )
                            },
                        )
            removed_runs = connection.execute(
                """
                SELECT r.run_id, p.repository_name, p.number, p.stage
                FROM runs AS r
                JOIN pbis AS p
                  ON p.project_id = r.project_id
                 AND p.repository_name = r.repository_name
                 AND p.number = r.pbi_number
                WHERE r.project_id = ? AND r.status = 'active' AND p.active = 0
                """,
                (snapshot.project_id,),
            ).fetchall()
            for removed_run in removed_runs:
                removed_run_ids.append(str(removed_run["run_id"]))
                connection.execute(
                    """
                    UPDATE runs
                    SET status = 'failed', execution_token = NULL,
                        last_error = ?, lease_token = NULL,
                        lease_expires_at = NULL, updated_at = ?
                    WHERE run_id = ?
                    """,
                    (
                        "PBI is no longer linked to the selected project",
                        _now(),
                        removed_run["run_id"],
                    ),
                )
                connection.execute(
                    """
                    UPDATE pbis SET last_error = ?
                    WHERE project_id = ? AND repository_name = ? AND number = ?
                    """,
                    (
                        "PBI is no longer linked to the selected project",
                        snapshot.project_id,
                        removed_run["repository_name"],
                        removed_run["number"],
                    ),
                )
                self._record_event(
                    connection,
                    snapshot.project_id,
                    str(removed_run["repository_name"]),
                    int(removed_run["number"]),
                    str(removed_run["run_id"]),
                    "removed",
                    Stage(str(removed_run["stage"])),
                    Stage(str(removed_run["stage"])),
                    {"reason": "pbi no longer linked to selected project"},
                )
        return tuple(removed_run_ids)

    def claim_next(
        self,
        project_id: str,
        repository: str,
        owner_id: str,
        lease_token: str | None = None,
        *,
        expected_run_id: str | None = None,
        agent_session: tuple[str, str] | None = None,
    ) -> RunState | None:
        if not owner_id.strip():
            raise StoreError("A worker owner is required")
        if agent_session is not None and (
            not agent_session[0].strip() or not agent_session[1].strip()
        ):
            raise StoreError("An agent worker and task are required")
        with self._transaction() as connection:
            active = connection.execute(
                """
                SELECT p.*, r.run_id, r.status, r.attempt,
                       r.owner_id, r.lease_token, r.lease_expires_at,
                       r.last_error AS run_error, r.last_result AS run_result,
                       r.task_contract_json, r.task_result_json, r.task_answer
                FROM pbis AS p
                JOIN repositories AS repository
                  ON repository.project_id = p.project_id
                 AND repository.name = p.repository_name
                 AND repository.active = 1
                 AND p.active = 1
                JOIN runs AS r
                  ON r.project_id = p.project_id
                 AND r.repository_name = p.repository_name
                 AND r.pbi_number = p.number
                WHERE p.project_id = ? AND p.repository_name = ?
                  AND r.status = 'active'
                  AND (? IS NULL OR r.run_id = ?)
                LIMIT 1
                """,
                (project_id, repository, expected_run_id, expected_run_id),
            ).fetchone()
            if expected_run_id is not None and active is None:
                return None
            if active is not None:
                active_run = self._run_from_row(active)
                if (
                    active_run.owner_id == owner_id
                    and lease_token is not None
                    and active_run.lease_token == lease_token
                    and _lease_is_active(active_run.lease_expires_at)
                ):
                    self._renew_lease(connection, active_run.run_id, lease_token)
                    if agent_session is not None:
                        self._upsert_agent_session(
                            connection, active_run.run_id, *agent_session
                        )
                    return self._run_for_id(connection, active_run.run_id)
                if _lease_is_active(active_run.lease_expires_at):
                    return None
                run_id = active_run.run_id
                new_lease_token = str(uuid4())
                connection.execute(
                    """
                    UPDATE runs
                    SET owner_id = ?, lease_token = ?, lease_expires_at = ?,
                        execution_token = NULL, last_error = NULL,
                        last_result = NULL, updated_at = ?
                    WHERE run_id = ? AND status = 'active'
                    """,
                    (
                        owner_id,
                        new_lease_token,
                        self._lease_deadline(),
                        _now(),
                        run_id,
                    ),
                )
                self._record_event(
                    connection,
                    project_id,
                    repository,
                    int(active["number"]),
                    run_id,
                    "lease_reclaimed",
                    Stage(str(active["stage"])),
                    Stage(str(active["stage"])),
                    {"owner_id": owner_id},
                )
                if agent_session is not None:
                    self._upsert_agent_session(connection, run_id, *agent_session)
                return self._run_for_id(connection, run_id)

            candidate = connection.execute(
                """
                SELECT p.*, r.run_id AS existing_run_id, r.status AS existing_status,
                       r.attempt AS existing_attempt
                FROM pbis AS p
                JOIN repositories AS repository
                  ON repository.project_id = p.project_id
                 AND repository.name = p.repository_name
                 AND repository.active = 1
                 AND p.active = 1
                LEFT JOIN runs AS r
                  ON r.project_id = p.project_id
                 AND r.repository_name = p.repository_name
                 AND r.pbi_number = p.number
                WHERE p.project_id = ?
                  AND p.repository_name = ?
                  AND p.claimable = 1
                  AND p.stage != ?
                  AND (r.status IS NULL OR r.status = 'failed')
                ORDER BY p.number
                LIMIT 1
                """,
                (project_id, repository, Stage.PULL_REQUEST.value),
            ).fetchone()
            if candidate is None:
                return None

            run_id = candidate["existing_run_id"]
            new_lease_token = str(uuid4())
            if isinstance(run_id, str):
                connection.execute(
                    """
                    UPDATE runs
                    SET status = 'active', attempt = attempt + 1,
                        owner_id = ?, lease_token = ?, lease_expires_at = ?,
                        execution_token = NULL, last_error = NULL,
                        last_result = NULL, updated_at = ?
                    WHERE run_id = ?
                    """,
                    (
                        owner_id,
                        new_lease_token,
                        self._lease_deadline(),
                        _now(),
                        run_id,
                    ),
                )
                event_type = "resumed"
            else:
                run_id = str(uuid4())
                connection.execute(
                    """
                    INSERT INTO runs(
                        run_id, project_id, repository_name, pbi_number,
                        status, attempt, owner_id, lease_token,
                        lease_expires_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'active', 1, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        project_id,
                        repository,
                        candidate["number"],
                        owner_id,
                        new_lease_token,
                        self._lease_deadline(),
                        _now(),
                    ),
                )
                event_type = "claimed"

            current_stage = Stage(candidate["stage"])
            if current_stage is Stage.BACKLOG:
                connection.execute(
                    """
                    UPDATE pbis SET stage = ?, run_id = ?, last_error = NULL
                    WHERE project_id = ? AND repository_name = ? AND number = ?
                    """,
                    (
                        Stage.REFINE.value,
                        run_id,
                        project_id,
                        repository,
                        candidate["number"],
                    ),
                )
                self._record_event(
                    connection,
                    project_id,
                    repository,
                    candidate["number"],
                    run_id,
                    "transition",
                    Stage.BACKLOG,
                    Stage.REFINE,
                    {},
                )
            else:
                connection.execute(
                    """
                    UPDATE pbis SET run_id = ?, last_error = NULL
                    WHERE project_id = ? AND repository_name = ? AND number = ?
                    """,
                    (run_id, project_id, repository, candidate["number"]),
                )
                self._record_event(
                    connection,
                    project_id,
                    repository,
                    candidate["number"],
                    run_id,
                    event_type,
                    current_stage,
                    current_stage,
                    {},
                )
            if agent_session is not None:
                self._upsert_agent_session(connection, run_id, *agent_session)
            return self._run_for_id(connection, run_id)

    def advance(self, run_id: str, target: Stage, lease_token: str) -> RunState:
        with self._transaction() as connection:
            row = self._run_for_id(connection, run_id)
            if row is None:
                raise StoreError(f"Unknown run: {run_id}")
            self._require_lease(row, lease_token)
            if row.status is not RunStatus.ACTIVE:
                if row.stage is target:
                    return row
                raise StoreError(f"Run {run_id} is {row.status.value}, not active")
            if row.stage is target:
                return row
            allowed = {
                Stage.REFINE: Stage.IMPLEMENT,
            }
            if allowed.get(row.stage) is not target:
                raise StoreError(
                    f"Cannot advance {row.stage.value} directly to {target.value}"
                )
            connection.execute(
                """
                UPDATE pbis SET stage = ?, last_error = NULL
                WHERE project_id = ? AND repository_name = ? AND number = ?
                """,
                (target.value, row.project_id, row.repository, row.pbi_number),
            )
            self._renew_lease(connection, run_id, lease_token)
            self._record_event(
                connection,
                row.project_id,
                row.repository,
                row.pbi_number,
                run_id,
                "transition",
                row.stage,
                target,
                {},
            )
            return self._run_for_id(connection, run_id) or row

    def record_handoff(
        self,
        run_id: str,
        branch: str,
        pull_request_url: str,
        pull_request_number: int | None,
        lease_token: str,
        result: str | None = None,
    ) -> RunState:
        if not branch.strip() or not pull_request_url.strip():
            raise StoreError("A branch and pull-request URL are required")
        normalized_result = result.strip()[:MAX_AGENT_RESULT_LENGTH] if result else None
        with self._transaction() as connection:
            row = self._run_for_id(connection, run_id)
            if row is None:
                raise StoreError(f"Unknown run: {run_id}")
            self._require_lease(row, lease_token)
            if row.status is RunStatus.COMPLETED and row.stage is Stage.PULL_REQUEST:
                return row
            if row.status is not RunStatus.ACTIVE or row.stage is not Stage.IMPLEMENT:
                raise StoreError(
                    "Only an active implementation run can create a handoff"
                )
            intent = connection.execute(
                """
                SELECT branch, handoff_status
                FROM pbis
                WHERE project_id = ? AND repository_name = ? AND number = ?
                """,
                (row.project_id, row.repository, row.pbi_number),
            ).fetchone()
            if (
                intent is None
                or intent["handoff_status"] != "pending"
                or intent["branch"] != branch
            ):
                raise StoreError("Handoff does not match the persisted intent")
            connection.execute(
                """
                UPDATE pbis
                SET stage = ?, branch = ?, pull_request_url = ?,
                    handoff_status = 'completed', claimable = 0, last_error = NULL
                WHERE project_id = ? AND repository_name = ? AND number = ?
                """,
                (
                    Stage.PULL_REQUEST.value,
                    branch,
                    pull_request_url,
                    row.project_id,
                    row.repository,
                    row.pbi_number,
                ),
            )
            connection.execute(
                """
                UPDATE runs
                SET status = 'completed', execution_token = NULL,
                    last_error = NULL, last_result = COALESCE(?, last_result),
                    updated_at = ?
                WHERE run_id = ?
                """,
                (normalized_result, _now(), run_id),
            )
            connection.execute(
                """
                INSERT INTO handoffs(
                    run_id, project_id, repository_name, pbi_number, branch,
                    pull_request_url, pull_request_number, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    row.project_id,
                    row.repository,
                    row.pbi_number,
                    branch,
                    pull_request_url,
                    pull_request_number,
                    _now(),
                ),
            )
            self._record_event(
                connection,
                row.project_id,
                row.repository,
                row.pbi_number,
                run_id,
                "transition",
                Stage.IMPLEMENT,
                Stage.PULL_REQUEST,
                {"branch": branch, "pull_request_url": pull_request_url},
            )
            return self._run_for_id(connection, run_id) or row

    def prepare_handoff(
        self,
        run_id: str,
        branch: str,
        base_branch: str | None,
        body: str,
        lease_token: str,
        head_sha: str | None = None,
        verification_evidence: str = "",
    ) -> HandoffIntent:
        if not branch.strip():
            raise StoreError("A branch is required")
        with self._transaction() as connection:
            row = self._run_for_id(connection, run_id)
            if row is None:
                raise StoreError(f"Unknown run: {run_id}")
            self._require_lease(row, lease_token)
            persisted = connection.execute(
                """
                SELECT branch, handoff_base_branch, handoff_body,
                       handoff_head_sha, handoff_verification_evidence,
                       handoff_status
                FROM pbis
                WHERE project_id = ? AND repository_name = ? AND number = ?
                """,
                (row.project_id, row.repository, row.pbi_number),
            ).fetchone()
            if persisted is None:
                raise StoreError(f"Unknown PBI for run: {run_id}")
            if row.status is RunStatus.COMPLETED and row.stage is Stage.PULL_REQUEST:
                return HandoffIntent(
                    row,
                    str(persisted["branch"]),
                    persisted["handoff_base_branch"],
                    str(persisted["handoff_body"] or ""),
                    persisted["handoff_head_sha"],
                    str(persisted["handoff_verification_evidence"] or ""),
                )
            if row.status is not RunStatus.ACTIVE or row.stage is not Stage.IMPLEMENT:
                raise StoreError(
                    "Only an active implementation run can create a handoff"
                )
            if persisted["handoff_status"] == "pending":
                if (
                    persisted["branch"] != branch
                    or persisted["handoff_base_branch"] != base_branch
                    or persisted["handoff_body"] != body
                    or persisted["handoff_head_sha"] != head_sha
                    or str(persisted["handoff_verification_evidence"] or "")
                    != verification_evidence
                ):
                    raise StoreError(
                        "Handoff request does not match the persisted intent"
                    )
                self._renew_lease(connection, run_id, lease_token)
                renewed_row = self._run_for_id(connection, run_id)
                if renewed_row is None:
                    raise StoreError(f"Unknown run: {run_id}")
                return HandoffIntent(
                    renewed_row,
                    str(persisted["branch"]),
                    persisted["handoff_base_branch"],
                    str(persisted["handoff_body"]),
                    persisted["handoff_head_sha"],
                    str(persisted["handoff_verification_evidence"] or ""),
                )
            if persisted["handoff_status"] == "completed":
                raise StoreError("Handoff intent is already completed")
            connection.execute(
                """
                UPDATE pbis
                SET branch = ?, handoff_base_branch = ?, handoff_body = ?,
                    handoff_head_sha = ?, handoff_verification_evidence = ?,
                    handoff_status = 'pending'
                WHERE project_id = ? AND repository_name = ? AND number = ?
                """,
                (
                    branch,
                    base_branch,
                    body,
                    head_sha,
                    verification_evidence,
                    row.project_id,
                    row.repository,
                    row.pbi_number,
                ),
            )
            self._renew_lease(connection, run_id, lease_token)
            persisted_row = self._run_for_id(connection, run_id)
            if persisted_row is None:
                raise StoreError(f"Unknown run: {run_id}")
            return HandoffIntent(
                persisted_row,
                branch,
                base_branch,
                body,
                head_sha,
                verification_evidence,
            )

    def pending_handoff(self, run_id: str, lease_token: str) -> HandoffIntent | None:
        with self._lock:
            run = self._run_for_id(self._connection, run_id)
            if run is None:
                raise StoreError(f"Unknown run: {run_id}")
            self._require_lease(run, lease_token)
            row = self._connection.execute(
                """
                SELECT branch, handoff_base_branch, handoff_body,
                       handoff_head_sha, handoff_verification_evidence,
                       handoff_status
                FROM pbis
                WHERE project_id = ? AND repository_name = ? AND number = ?
                """,
                (run.project_id, run.repository, run.pbi_number),
            ).fetchone()
            if row is None or row["handoff_status"] != "pending":
                return None
            return HandoffIntent(
                run,
                str(row["branch"]),
                row["handoff_base_branch"],
                str(row["handoff_body"] or ""),
                row["handoff_head_sha"],
                str(row["handoff_verification_evidence"] or ""),
            )

    def renew_lease(self, run_id: str, lease_token: str) -> RunState:
        with self._transaction() as connection:
            row = self._run_for_id(connection, run_id)
            if row is None:
                raise StoreError(f"Unknown run: {run_id}")
            self._require_lease(row, lease_token)
            if row.status is not RunStatus.ACTIVE:
                return row
            self._renew_lease(connection, run_id, lease_token)
            return self._run_for_id(connection, run_id) or row

    def validate_lease(self, run_id: str, lease_token: str) -> RunState:
        with self._lock:
            run = self._run_for_id(self._connection, run_id)
            if run is None:
                raise StoreError(f"Unknown run: {run_id}")
            self._require_lease(run, lease_token)
            return run

    def claim_execution(self, run_id: str, lease_token: str) -> str:
        with self._transaction() as connection:
            row = self._run_for_id(connection, run_id)
            if row is None:
                raise StoreError(f"Unknown run: {run_id}")
            self._require_lease(row, lease_token)
            if row.status is not RunStatus.ACTIVE or row.stage is not Stage.IMPLEMENT:
                raise StoreError(
                    "Only an active implementation run can execute a model"
                )
            existing = connection.execute(
                "SELECT execution_token FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if existing is None:
                raise StoreError(f"Unknown run: {run_id}")
            if existing["execution_token"] is not None:
                raise StoreError("A model execution is already active")
            execution_token = str(uuid4())
            connection.execute(
                """
                UPDATE runs
                SET execution_token = ?, updated_at = ?
                WHERE run_id = ? AND status = 'active' AND lease_token = ?
                """,
                (execution_token, _now(), run_id, lease_token),
            )
            self._renew_lease(connection, run_id, lease_token)
            return execution_token

    def validate_execution(
        self, run_id: str, lease_token: str, execution_token: str
    ) -> None:
        with self._lock:
            run = self._run_for_id(self._connection, run_id)
            if run is None:
                raise StoreError(f"Unknown run: {run_id}")
            self._require_lease(run, lease_token)
            current = self._connection.execute(
                "SELECT execution_token FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if current is None or current["execution_token"] != execution_token:
                raise StoreError("Model execution claim is no longer valid")

    def heartbeat_execution(
        self, run_id: str, lease_token: str, execution_token: str
    ) -> None:
        with self._transaction() as connection:
            current = connection.execute(
                """
                SELECT status, lease_token, execution_token
                FROM runs WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
            if current is None:
                raise StoreError(f"Unknown run: {run_id}")
            if (
                current["status"] != RunStatus.ACTIVE.value
                or current["lease_token"] != lease_token
                or current["execution_token"] != execution_token
            ):
                raise StoreError("Model execution claim is no longer valid")
            self._renew_lease(connection, run_id, lease_token)

    def release_execution(self, run_id: str, execution_token: str) -> None:
        with self._transaction() as connection:
            connection.execute(
                """
                UPDATE runs
                SET execution_token = NULL, updated_at = ?
                WHERE run_id = ? AND execution_token = ?
                """,
                (_now(), run_id, execution_token),
            )

    def ensure_task_contract(
        self, run_id: str, contract: TaskContract, lease_token: str
    ) -> RunState:
        try:
            contract_data = contract.as_dict()
        except ContractError as exc:
            raise StoreError(str(exc)) from exc
        with self._transaction() as connection:
            row = self._run_for_id(connection, run_id)
            if row is None:
                raise StoreError(f"Unknown run: {run_id}")
            self._require_lease(row, lease_token)
            if row.status is not RunStatus.ACTIVE or row.stage is not Stage.IMPLEMENT:
                raise StoreError(
                    "Only an active implementation run can set a task contract"
                )
            if row.task_contract == contract_data:
                return row
            connection.execute(
                """
                UPDATE runs
                SET task_contract_json = ?, task_result_json = NULL,
                    task_answer_resumed = 0, updated_at = ?
                WHERE run_id = ?
                """,
                (json.dumps(contract_data, sort_keys=True), _now(), run_id),
            )
            self._record_event(
                connection,
                row.project_id,
                row.repository,
                row.pbi_number,
                run_id,
                "task_contract",
                row.stage,
                row.stage,
                {"contract": contract_data},
            )
            return self._run_for_id(connection, run_id) or row

    def record_task_result(
        self, run_id: str, result: TaskResult, lease_token: str
    ) -> RunState:
        with self._transaction() as connection:
            row = self._run_for_id(connection, run_id)
            if row is None:
                raise StoreError(f"Unknown run: {run_id}")
            self._require_lease(row, lease_token)
            if row.status is not RunStatus.ACTIVE or row.stage is not Stage.IMPLEMENT:
                raise StoreError(
                    "Only an active implementation run can record a task result"
                )
            if row.task_contract is None:
                raise StoreError("A task contract is required before its result")
            try:
                contract = TaskContract.from_dict(row.task_contract)
                validated = result.validated(contract)
                if validated.answer is not None:
                    raise ContractError("Worker task results cannot contain an answer")
                result_data = validated.as_dict()
            except ContractError as exc:
                raise StoreError(str(exc)) from exc
            connection.execute(
                """
                UPDATE runs SET task_result_json = ?, task_answer_resumed = 0,
                    updated_at = ?
                WHERE run_id = ?
                """,
                (json.dumps(result_data, sort_keys=True), _now(), run_id),
            )
            self._record_event(
                connection,
                row.project_id,
                row.repository,
                row.pbi_number,
                run_id,
                "task_result",
                row.stage,
                row.stage,
                {"result": result_data},
            )
            return self._run_for_id(connection, run_id) or row

    def answer_task_question(self, run_id: str, answer: str) -> RunState:
        if not answer.strip():
            raise StoreError("An answer is required")
        with self._transaction() as connection:
            row = self._run_for_id(connection, run_id)
            if row is None:
                raise StoreError(f"Unknown run: {run_id}")
            if row.task_contract is None or row.task_result is None:
                raise StoreError("Run has no pending task question")
            try:
                contract = TaskContract.from_dict(row.task_contract)
                current = TaskResult.from_payload(
                    row.task_result, contract, allow_answer=True
                )
            except ContractError as exc:
                raise StoreError(str(exc)) from exc
            if current.outcome is not TaskOutcome.QUESTION:
                raise StoreError("Run has no pending task question")
            answered = TaskResult(
                current.outcome,
                current.evidence,
                current.artifact_refs,
                current.question,
                current.required_action,
                current.validation_reason,
                answer,
            ).validated(contract)
            if current.answer is not None:
                if current.answer == answered.answer:
                    connection.execute(
                        """
                        UPDATE runs SET task_answer_resumed = 0, updated_at = ?
                        WHERE run_id = ?
                        """,
                        (_now(), run_id),
                    )
                    connection.execute(
                        """
                        UPDATE pbis SET claimable = 0
                        WHERE project_id = ? AND repository_name = ? AND number = ?
                        """,
                        (row.project_id, row.repository, row.pbi_number),
                    )
                    return self._run_for_id(connection, run_id) or row
                raise StoreError("A different answer is already recorded")
            result_data = answered.as_dict()
            connection.execute(
                """
                UPDATE runs
                SET task_result_json = ?, task_answer = ?, last_error = NULL,
                    task_answer_resumed = 0, updated_at = ?
                WHERE run_id = ?
                """,
                (
                    json.dumps(result_data, sort_keys=True),
                    cast(str, answered.answer),
                    _now(),
                    run_id,
                ),
            )
            connection.execute(
                """
                UPDATE pbis SET claimable = 0, last_error = NULL
                WHERE project_id = ? AND repository_name = ? AND number = ?
                """,
                (row.project_id, row.repository, row.pbi_number),
            )
            self._record_event(
                connection,
                row.project_id,
                row.repository,
                row.pbi_number,
                run_id,
                "question_answered",
                row.stage,
                row.stage,
                {"answer": cast(str, answered.answer)},
            )
            return self._run_for_id(connection, run_id) or row

    def mark_task_question_resumed(self, run_id: str) -> RunState:
        with self._transaction() as connection:
            row = self._run_for_id(connection, run_id)
            if row is None:
                raise StoreError(f"Unknown run: {run_id}")
            if row.task_contract is None or row.task_result is None:
                raise StoreError("Run has no answered task question")
            try:
                contract = TaskContract.from_dict(row.task_contract)
                result = TaskResult.from_payload(
                    row.task_result, contract, allow_answer=True
                )
            except ContractError as exc:
                raise StoreError(str(exc)) from exc
            if (
                result.outcome is not TaskOutcome.QUESTION
                or result.answer is None
                or result.answer != row.task_answer
            ):
                raise StoreError("Run has no answered task question")
            previous = connection.execute(
                "SELECT task_answer_resumed FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            connection.execute(
                """
                UPDATE runs SET task_answer_resumed = 1, updated_at = ?
                WHERE run_id = ?
                """,
                (_now(), run_id),
            )
            connection.execute(
                """
                UPDATE pbis SET claimable = ?, last_error = NULL
                WHERE project_id = ? AND repository_name = ? AND number = ?
                """,
                (
                    int(row.stage is not Stage.PULL_REQUEST),
                    row.project_id,
                    row.repository,
                    row.pbi_number,
                ),
            )
            if previous is not None and not bool(previous["task_answer_resumed"]):
                self._record_event(
                    connection,
                    row.project_id,
                    row.repository,
                    row.pbi_number,
                    run_id,
                    "question_resumed",
                    row.stage,
                    row.stage,
                    {},
                )
            return self._run_for_id(connection, run_id) or row

    @property
    def lease_heartbeat_seconds(self) -> float:
        return max(0.05, self._lease_seconds / 3)

    def fail(self, run_id: str, error: str, lease_token: str) -> RunState:
        failed, _ = self.fail_with_transition(run_id, error, lease_token)
        return failed

    def fail_with_transition(
        self,
        run_id: str,
        error: str,
        lease_token: str,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        recursive_spawn_depth: int = 0,
        route_failure: bool = False,
    ) -> tuple[RunState, bool]:
        if not error.strip():
            raise StoreError("A failure reason is required")
        if input_tokens < 0 or output_tokens < 0:
            raise StoreError("Token usage must not be negative")
        if recursive_spawn_depth < 0:
            raise StoreError("Recursive spawn depth must not be negative")
        with self._transaction() as connection:
            row = self._run_for_id(connection, run_id)
            if row is None:
                raise StoreError(f"Unknown run: {run_id}")
            self._require_lease(row, lease_token)
            if row.status is RunStatus.COMPLETED:
                raise StoreError("A completed run cannot fail")
            if row.status is RunStatus.FAILED:
                return row, False
            connection.execute(
                """
                UPDATE runs
                SET status = 'failed', execution_token = NULL,
                    last_error = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (error, _now(), run_id),
            )
            connection.execute(
                """
                UPDATE pbis SET last_error = ?
                WHERE project_id = ? AND repository_name = ? AND number = ?
                """,
                (error, row.project_id, row.repository, row.pbi_number),
            )
            self._record_event(
                connection,
                row.project_id,
                row.repository,
                row.pbi_number,
                run_id,
                "failure",
                row.stage,
                row.stage,
                {"error": error},
            )
            if route_failure and row.stage is Stage.IMPLEMENT:
                connection.execute(
                    """
                    INSERT INTO routing_failure_outbox(
                        transition_id, run_id, error, input_tokens, output_tokens,
                        recursive_spawn_depth, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid4()),
                        run_id,
                        error,
                        input_tokens,
                        output_tokens,
                        recursive_spawn_depth,
                        _now(),
                    ),
                )
            return self._run_for_id(connection, run_id) or row, True

    def complete_agent_run(
        self, run_id: str, result: str, lease_token: str
    ) -> RunState:
        """Persist a bounded worker result and release its lease."""

        normalized_result = result.strip()
        if not normalized_result:
            raise StoreError("An agent result is required")
        normalized_result = normalized_result[:MAX_AGENT_RESULT_LENGTH]
        with self._transaction() as connection:
            row = self._run_for_id(connection, run_id)
            if row is None:
                raise StoreError(f"Unknown run: {run_id}")
            if row.status is RunStatus.COMPLETED:
                return row
            self._require_lease(row, lease_token)
            if row.status is not RunStatus.ACTIVE or row.stage is not Stage.IMPLEMENT:
                raise StoreError("Only an active implementation run can complete")
            task_result_data = row.task_result
            if row.task_contract is None:
                raise StoreError("A task contract is required before completion")
            if task_result_data is None:
                raise StoreError("A task result is required before completion")
            try:
                task_result = TaskResult.from_payload(
                    task_result_data, TaskContract.from_dict(row.task_contract)
                )
            except ContractError as exc:
                raise StoreError(str(exc)) from exc
            if task_result.outcome is not TaskOutcome.PASS:
                raise StoreError("Only a passing task result can complete")
            now = _now()
            connection.execute(
                """
                UPDATE runs
                SET status = 'completed', execution_token = NULL,
                    last_error = NULL, last_result = ?, lease_token = NULL,
                    lease_expires_at = NULL, updated_at = ?
                WHERE run_id = ?
                """,
                (normalized_result, now, run_id),
            )
            connection.execute(
                """
                UPDATE pbis SET claimable = 0, last_error = NULL
                WHERE project_id = ? AND repository_name = ? AND number = ?
                """,
                (row.project_id, row.repository, row.pbi_number),
            )
            self._record_event(
                connection,
                row.project_id,
                row.repository,
                row.pbi_number,
                run_id,
                "completed",
                row.stage,
                row.stage,
                {
                    "result": normalized_result,
                    "task_result": task_result_data,
                },
            )
            return self._run_for_id(connection, run_id) or row

    def fail_agent_run(
        self,
        run_id: str,
        error: str,
        lease_token: str,
        *,
        claimable: bool | None = None,
    ) -> RunState:
        """Persist a worker failure and release its lease for a later retry."""

        normalized_error = error.strip()
        if not normalized_error:
            raise StoreError("A failure reason is required")
        normalized_error = normalized_error[:MAX_AGENT_RESULT_LENGTH]
        with self._transaction() as connection:
            row = self._run_for_id(connection, run_id)
            if row is None:
                raise StoreError(f"Unknown run: {run_id}")
            if row.status is RunStatus.COMPLETED:
                raise StoreError("A completed run cannot fail")
            if row.status is RunStatus.FAILED:
                return row
            self._require_lease(row, lease_token)
            task_paused, answered_question = _task_claimability_for_run(
                connection, run_id
            )
            requested_claimable = (
                row.stage is not Stage.PULL_REQUEST if claimable is None else claimable
            )
            effective_claimable = (
                requested_claimable or answered_question
            ) and not task_paused
            now = _now()
            connection.execute(
                """
                UPDATE runs
                SET status = 'failed', execution_token = NULL,
                    last_error = ?, last_result = NULL, lease_token = NULL,
                    lease_expires_at = NULL, updated_at = ?
                WHERE run_id = ?
                """,
                (normalized_error, now, run_id),
            )
            connection.execute(
                """
                UPDATE pbis SET claimable = ?, last_error = ?
                WHERE project_id = ? AND repository_name = ? AND number = ?
                """,
                (
                    int(effective_claimable),
                    None if answered_question else normalized_error,
                    row.project_id,
                    row.repository,
                    row.pbi_number,
                ),
            )
            details: dict[str, object] = {"error": normalized_error}
            if answered_question:
                details["superseded_by_answer"] = True
            self._record_event(
                connection,
                row.project_id,
                row.repository,
                row.pbi_number,
                run_id,
                "failure",
                row.stage,
                row.stage,
                details,
            )
            return self._run_for_id(connection, run_id) or row

    def fail_agent_run_after_lease_loss(
        self, run_id: str, error: str, *, expected_lease_token: str
    ) -> RunState:
        """Record a worker failure even when its lease can no longer be used."""

        normalized_error = error.strip()
        if not normalized_error:
            raise StoreError("A failure reason is required")
        if not expected_lease_token.strip():
            raise StoreError("A run lease token is required")
        normalized_error = normalized_error[:MAX_AGENT_RESULT_LENGTH]
        with self._transaction() as connection:
            row = self._run_for_id(connection, run_id)
            if row is None:
                raise StoreError(f"Unknown run: {run_id}")
            if row.status in {RunStatus.COMPLETED, RunStatus.FAILED}:
                return row
            task_paused, answered_question = _task_claimability_for_run(
                connection, run_id
            )
            effective_claimable = (
                row.stage is not Stage.PULL_REQUEST and not task_paused
            )
            now = _now()
            updated = connection.execute(
                """
                UPDATE runs
                SET status = 'failed', execution_token = NULL,
                    last_error = ?, last_result = NULL, lease_token = NULL,
                    lease_expires_at = NULL, updated_at = ?
                WHERE run_id = ? AND status = 'active' AND lease_token = ?
                """,
                (normalized_error, now, run_id, expected_lease_token),
            )
            if updated.rowcount == 0:
                return self._run_for_id(connection, run_id) or row
            connection.execute(
                """
                UPDATE pbis SET claimable = ?, last_error = ?
                WHERE project_id = ? AND repository_name = ? AND number = ?
                """,
                (
                    int(effective_claimable),
                    None if answered_question else normalized_error,
                    row.project_id,
                    row.repository,
                    row.pbi_number,
                ),
            )
            details: dict[str, object] = {
                "error": normalized_error,
                "lease_lost": True,
            }
            if answered_question:
                details["superseded_by_answer"] = True
            self._record_event(
                connection,
                row.project_id,
                row.repository,
                row.pbi_number,
                run_id,
                "failure",
                row.stage,
                row.stage,
                details,
            )
            return self._run_for_id(connection, run_id) or row

    def set_run_claimable(self, run_id: str, claimable: bool) -> None:
        with self._transaction() as connection:
            row = self._run_for_id(connection, run_id)
            if row is None:
                raise StoreError(f"Unknown run: {run_id}")
            connection.execute(
                """
                UPDATE pbis SET claimable = ?
                WHERE project_id = ? AND repository_name = ? AND number = ?
                """,
                (
                    int(claimable),
                    row.project_id,
                    row.repository,
                    row.pbi_number,
                ),
            )

    def pending_routing_failures(self, run_id: str) -> tuple[RoutingFailure, ...]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT transition_id, run_id, error, input_tokens, output_tokens,
                       recursive_spawn_depth
                FROM routing_failure_outbox
                WHERE run_id = ? AND status = 'pending'
                ORDER BY created_at, transition_id
                """,
                (run_id,),
            ).fetchall()
        return tuple(
            RoutingFailure(
                transition_id=str(row["transition_id"]),
                run_id=str(row["run_id"]),
                error=str(row["error"]),
                input_tokens=int(row["input_tokens"]),
                output_tokens=int(row["output_tokens"]),
                recursive_spawn_depth=int(row["recursive_spawn_depth"]),
            )
            for row in rows
        )

    def mark_routing_failure_processed(self, transition_id: str) -> None:
        with self._transaction() as connection:
            connection.execute(
                """
                UPDATE routing_failure_outbox
                SET status = 'processed', processed_at = ?
                WHERE transition_id = ? AND status = 'pending'
                """,
                (_now(), transition_id),
            )

    def stop(self, run_id: str, reason: str = "Stopped by operator") -> RunState:
        """Stop a run from an authenticated operator action."""

        if not reason.strip():
            raise StoreError("A stop reason is required")
        with self._transaction() as connection:
            row = self._run_for_id(connection, run_id)
            if row is None:
                raise StoreError(f"Unknown run: {run_id}")
            if row.status in {RunStatus.COMPLETED, RunStatus.FAILED}:
                return row
            connection.execute(
                """
                UPDATE runs
                SET status = 'failed', execution_token = NULL,
                    last_error = ?, lease_token = NULL,
                    lease_expires_at = NULL, updated_at = ?
                WHERE run_id = ?
                """,
                (reason, _now(), run_id),
            )
            connection.execute(
                """
                UPDATE pbis SET last_error = ?
                WHERE project_id = ? AND repository_name = ? AND number = ?
                """,
                (reason, row.project_id, row.repository, row.pbi_number),
            )
            self._record_event(
                connection,
                row.project_id,
                row.repository,
                row.pbi_number,
                run_id,
                "stopped",
                row.stage,
                row.stage,
                {"reason": reason},
            )
            return self._run_for_id(connection, run_id) or row

    def begin_action(
        self,
        project_id: str,
        kind: str,
        request: dict[str, object],
        repository: str | None = None,
        pbi_number: int | None = None,
        run_id: str | None = None,
    ) -> dict[str, object]:
        """Persist an operator action before its side effect starts."""

        if not project_id.strip() or not kind.strip():
            raise StoreError("An action project and kind are required")
        action_id = str(uuid4())
        now = _now()
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO actions(
                    action_id, project_id, kind, status, repository_name,
                    pbi_number, run_id, request_json, created_at, updated_at
                ) VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?)
                """,
                (
                    action_id,
                    project_id,
                    kind,
                    repository,
                    pbi_number,
                    run_id,
                    json.dumps(request, sort_keys=True),
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM actions WHERE action_id = ?", (action_id,)
            ).fetchone()
            if row is None:
                raise StoreError(f"Could not create action: {action_id}")
            return self._action_from_row(row)

    def finish_action(
        self,
        action_id: str,
        status: str,
        result: dict[str, object] | None = None,
        error: str | None = None,
    ) -> dict[str, object]:
        """Record the observable result of an operator action."""

        if status not in {"succeeded", "failed"}:
            raise StoreError(f"Invalid action status: {status}")
        with self._transaction() as connection:
            connection.execute(
                """
                UPDATE actions
                SET status = ?, result_json = ?, error = ?, updated_at = ?
                WHERE action_id = ?
                """,
                (
                    status,
                    json.dumps(result, sort_keys=True) if result is not None else None,
                    error,
                    _now(),
                    action_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM actions WHERE action_id = ?", (action_id,)
            ).fetchone()
            if row is None:
                raise StoreError(f"Unknown action: {action_id}")
            return self._action_from_row(row)

    def actions_for_project(
        self, project_id: str, limit: int = DEFAULT_ACTION_LIMIT
    ) -> list[dict[str, object]]:
        """Return recent pending, successful, and failed dashboard actions."""

        if not 1 <= limit <= MAX_ACTION_LIMIT:
            raise StoreError(f"action_limit must be between 1 and {MAX_ACTION_LIMIT}")
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM actions
                WHERE project_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (project_id, limit),
            ).fetchall()
            return [self._action_from_row(row) for row in rows]

    def completed_session_records(
        self, project_id: str, since: str | None, limit: int
    ) -> tuple[dict[str, object], ...]:
        """Return bounded, durable completion records for meta-review."""

        if not project_id.strip():
            raise StoreError("A project id is required")
        if not 1 <= limit <= MAX_META_REVIEW_RECORDS:
            raise StoreError(
                f"record_limit must be between 1 and {MAX_META_REVIEW_RECORDS}"
            )
        with self._lock:
            query = """
                SELECT r.run_id, r.project_id, r.repository_name, r.pbi_number,
                       r.status, r.attempt, r.last_result, r.last_error,
                       r.updated_at, p.title
                FROM runs AS r
                JOIN pbis AS p
                  ON p.project_id = r.project_id
                 AND p.repository_name = r.repository_name
                 AND p.number = r.pbi_number
                WHERE r.project_id = ? AND r.status = 'completed'
            """
            parameters: list[object] = [project_id]
            if since is not None:
                query += " AND r.updated_at >= ?"
                parameters.append(since)
            query += " ORDER BY r.updated_at DESC, r.run_id LIMIT ?"
            parameters.append(limit)
            rows = self._connection.execute(query, parameters).fetchall()
            records: list[dict[str, object]] = []
            for row in rows:
                event_rows = self._connection.execute(
                    """
                    SELECT event_id, event_type, from_stage, to_stage,
                           details_json, created_at
                    FROM events
                    WHERE run_id = ?
                    ORDER BY event_id DESC
                    LIMIT 20
                    """,
                    (row["run_id"],),
                ).fetchall()
                records.append(
                    {
                        "source_id": f"run:{row['run_id']}",
                        "run_id": row["run_id"],
                        "project_id": row["project_id"],
                        "repository": row["repository_name"],
                        "pbi_number": row["pbi_number"],
                        "title": row["title"],
                        "status": row["status"],
                        "attempt": row["attempt"],
                        "result": row["last_result"],
                        "error": row["last_error"],
                        "updated_at": row["updated_at"],
                        "events": [
                            {
                                "id": event["event_id"],
                                "type": event["event_type"],
                                "from_stage": event["from_stage"],
                                "to_stage": event["to_stage"],
                                "details": _bounded_event_details(
                                    event["details_json"]
                                ),
                                "created_at": event["created_at"],
                            }
                            for event in reversed(event_rows)
                        ],
                    }
                )
            return tuple(records)

    def begin_meta_review(
        self,
        review_id: str,
        project_id: str,
        record_limit: int,
        input_token_limit: int,
    ) -> None:
        if not review_id.strip() or not project_id.strip():
            raise StoreError("A meta-review id and project id are required")
        with self._transaction() as connection:
            project = connection.execute(
                "SELECT 1 FROM projects WHERE project_id = ?", (project_id,)
            ).fetchone()
            if project is None:
                raise StoreError(f"Unknown project: {project_id}")
            now = _now()
            stale_before = (
                datetime.now(UTC) - timedelta(seconds=META_REVIEW_LEASE_SECONDS)
            ).isoformat()
            connection.execute(
                """
                UPDATE meta_review_runs
                SET status = 'failed', error = ?, completed_at = ?
                WHERE status = 'running' AND created_at < ?
                """,
                ("Meta-review lease expired", now, stale_before),
            )
            try:
                connection.execute(
                    """
                    INSERT INTO meta_review_runs(
                        review_id, project_id, status, record_limit,
                        input_token_limit, created_at
                    ) VALUES (?, ?, 'running', ?, ?, ?)
                    """,
                    (review_id, project_id, record_limit, input_token_limit, now),
                )
            except sqlite3.IntegrityError as exc:
                raise StoreError("A meta-review is already running") from exc

    def finish_meta_review(
        self,
        review_id: str,
        status: str,
        selected_records: int,
        input_tokens: int,
        missing_evidence: list[str],
        error: str | None = None,
    ) -> dict[str, object]:
        if status not in {"completed", "failed"}:
            raise StoreError(f"Invalid meta-review status: {status}")
        with self._transaction() as connection:
            connection.execute(
                """
                UPDATE meta_review_runs
                SET status = ?, selected_records = ?, input_tokens = ?,
                    missing_evidence_json = ?, error = ?, completed_at = ?
                WHERE review_id = ? AND status = 'running'
                """,
                (
                    status,
                    selected_records,
                    input_tokens,
                    json.dumps(missing_evidence, sort_keys=True),
                    error[:MAX_META_REVIEW_TEXT_LENGTH] if error else None,
                    _now(),
                    review_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM meta_review_runs WHERE review_id = ?", (review_id,)
            ).fetchone()
            if row is None:
                raise StoreError(f"Unknown meta-review: {review_id}")
            return self._meta_review_run_from_row(row)

    def complete_meta_review(
        self,
        review_id: str,
        selected_records: int,
        input_tokens: int,
        missing_evidence: list[str],
        suggestions: list[dict[str, object]],
    ) -> tuple[dict[str, object], list[dict[str, object]]]:
        with self._transaction() as connection:
            saved = self._save_meta_review_suggestions(
                connection, review_id, suggestions
            )
            now = _now()
            updated = connection.execute(
                """
                UPDATE meta_review_runs
                SET status = 'completed', selected_records = ?, input_tokens = ?,
                    missing_evidence_json = ?, completed_at = ?
                WHERE review_id = ? AND status = 'running'
                """,
                (
                    selected_records,
                    input_tokens,
                    json.dumps(missing_evidence, sort_keys=True),
                    now,
                    review_id,
                ),
            )
            if updated.rowcount != 1:
                raise StoreError("Meta-review is not running")
            row = connection.execute(
                "SELECT * FROM meta_review_runs WHERE review_id = ?", (review_id,)
            ).fetchone()
            if row is None:  # pragma: no cover - guarded by the update
                raise StoreError(f"Unknown meta-review: {review_id}")
            return self._meta_review_run_from_row(row), saved

    def save_meta_review_suggestions(
        self, review_id: str, suggestions: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        with self._transaction() as connection:
            return self._save_meta_review_suggestions(
                connection, review_id, suggestions
            )

    def _save_meta_review_suggestions(
        self,
        connection: sqlite3.Connection,
        review_id: str,
        suggestions: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        review = connection.execute(
            "SELECT project_id FROM meta_review_runs WHERE review_id = ?",
            (review_id,),
        ).fetchone()
        if review is None:
            raise StoreError(f"Unknown meta-review: {review_id}")
        project_id = str(review["project_id"])
        now = _now()
        for suggestion in suggestions:
            connection.execute(
                """
                INSERT INTO meta_review_suggestions(
                    suggestion_id, project_id, suggestion_key,
                    proposed_outcome, rationale, evidence_refs_json,
                    status, created_at, updated_at, last_review_id
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                ON CONFLICT(project_id, suggestion_key) DO UPDATE SET
                    proposed_outcome = excluded.proposed_outcome,
                    rationale = excluded.rationale,
                    evidence_refs_json = excluded.evidence_refs_json,
                    updated_at = excluded.updated_at,
                    last_review_id = excluded.last_review_id
                """,
                (
                    suggestion["suggestion_id"],
                    project_id,
                    suggestion["suggestion_key"],
                    suggestion["proposed_outcome"],
                    suggestion["rationale"],
                    json.dumps(suggestion["evidence_refs"], sort_keys=True),
                    now,
                    now,
                    review_id,
                ),
            )
        rows = connection.execute(
            """
            SELECT * FROM meta_review_suggestions
            WHERE project_id = ? AND last_review_id = ?
            ORDER BY created_at, suggestion_id
            """,
            (project_id, review_id),
        ).fetchall()
        return [self._meta_review_suggestion_from_row(row) for row in rows]

    def meta_review_suggestions(
        self, project_id: str, status: str | None = None
    ) -> list[dict[str, object]]:
        if not project_id.strip():
            raise StoreError("A project id is required")
        if status is not None and status not in {"pending", "accepted", "rejected"}:
            raise StoreError(f"Invalid suggestion status: {status}")
        with self._lock:
            query = "SELECT * FROM meta_review_suggestions WHERE project_id = ?"
            parameters: list[object] = [project_id]
            if status is not None:
                query += " AND status = ?"
                parameters.append(status)
            query += " ORDER BY created_at, suggestion_id LIMIT ?"
            parameters.append(MAX_META_REVIEW_SUGGESTIONS)
            rows = self._connection.execute(query, parameters).fetchall()
            return [self._meta_review_suggestion_from_row(row) for row in rows]

    def decide_meta_review_suggestion(
        self, project_id: str, suggestion_id: str, status: str
    ) -> dict[str, object]:
        if status not in {"accepted", "rejected"}:
            raise StoreError(f"Invalid suggestion decision: {status}")
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM meta_review_suggestions
                WHERE project_id = ? AND suggestion_id = ?
                """,
                (project_id, suggestion_id),
            ).fetchone()
            if row is None:
                raise StoreError(f"Unknown meta-review suggestion: {suggestion_id}")
            connection.execute(
                """
                UPDATE meta_review_suggestions
                SET status = ?, updated_at = ?
                WHERE project_id = ? AND suggestion_id = ?
                """,
                (status, _now(), project_id, suggestion_id),
            )
            updated = connection.execute(
                """
                SELECT * FROM meta_review_suggestions
                WHERE project_id = ? AND suggestion_id = ?
                """,
                (project_id, suggestion_id),
            ).fetchone()
            if updated is None:  # pragma: no cover - guarded by the update
                raise StoreError(f"Unknown meta-review suggestion: {suggestion_id}")
            return self._meta_review_suggestion_from_row(updated)

    def get_run(self, run_id: str) -> RunState | None:
        with self._lock:
            return self._run_for_id(self._connection, run_id)

    @staticmethod
    def _upsert_agent_session(
        connection: sqlite3.Connection, run_id: str, worker_id: str, task: str
    ) -> None:
        now = _now()
        connection.execute(
            """
            INSERT INTO agent_sessions(
                run_id, worker_id, task, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                worker_id = excluded.worker_id,
                updated_at = excluded.updated_at
            """,
            (
                run_id,
                worker_id.strip(),
                task,
                now,
                now,
            ),
        )

    def start_agent_session(
        self, run_id: str, worker_id: str, task: str, lease_token: str
    ) -> None:
        if not worker_id.strip() or not task.strip():
            raise StoreError("An agent worker and task are required")
        with self._transaction() as connection:
            run = self._run_for_id(connection, run_id)
            if run is None or run.status is not RunStatus.ACTIVE:
                raise StoreError("An active run is required for an agent session")
            self._require_lease(run, lease_token)
            self._upsert_agent_session(connection, run_id, worker_id, task)

    def record_agent_session_event(
        self,
        run_id: str,
        lease_token: str,
        kind: str,
        source_type: str,
        role: str | None,
        text: str,
    ) -> None:
        if kind not in {"progress", "message"} or not source_type.strip():
            raise StoreError("An agent event kind and source type are required")
        event_text = text[:MAX_AGENT_SESSION_EVENT_LENGTH]
        with self._transaction() as connection:
            run = self._run_for_id(connection, run_id)
            if run is None or run.status is not RunStatus.ACTIVE:
                raise StoreError("An active run is required for an agent event")
            self._require_lease(run, lease_token)
            if (
                connection.execute(
                    "SELECT 1 FROM agent_sessions WHERE run_id = ?", (run_id,)
                ).fetchone()
                is None
            ):
                raise StoreError("Agent session has not been started")
            sequence = int(
                connection.execute(
                    """
                    SELECT COALESCE(MAX(sequence), 0) + 1
                    FROM agent_session_events WHERE run_id = ?
                    """,
                    (run_id,),
                ).fetchone()[0]
            )
            timestamp = _now()
            connection.execute(
                """
                INSERT INTO agent_session_events(
                    run_id, sequence, kind, source_type, role, text, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    sequence,
                    kind,
                    source_type.strip()[:120],
                    role.strip()[:20] if role and role.strip() else None,
                    event_text,
                    timestamp,
                ),
            )
            connection.execute(
                "UPDATE agent_sessions SET updated_at = ? WHERE run_id = ?",
                (timestamp, run_id),
            )
            history = connection.execute(
                """
                SELECT sequence, length(CAST(text AS BLOB)) AS text_bytes
                FROM agent_session_events WHERE run_id = ? ORDER BY sequence
                """,
                (run_id,),
            ).fetchall()
            total_bytes = sum(int(row["text_bytes"]) for row in history)
            while (
                len(history) > MAX_AGENT_SESSION_EVENTS
                or total_bytes > MAX_AGENT_SESSION_TEXT_BYTES
            ):
                oldest = history.pop(0)
                total_bytes -= int(oldest["text_bytes"])
                connection.execute(
                    """
                    DELETE FROM agent_session_events
                    WHERE run_id = ? AND sequence = ?
                    """,
                    (run_id, oldest["sequence"]),
                )

    def get_agent_session(self, run_id: str) -> dict[str, object] | None:
        with self._lock:
            return self._agent_session_for_run(self._connection, run_id)

    def active_agent_sessions(self) -> tuple[RunState, ...]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT s.run_id FROM agent_sessions AS s
                JOIN runs AS r ON r.run_id = s.run_id
                WHERE r.status = 'active' ORDER BY s.created_at
                """
            ).fetchall()
            return tuple(
                run
                for row in rows
                if (run := self._run_for_id(self._connection, str(row["run_id"])))
                is not None
            )

    def _agent_session_for_run(
        self, connection: sqlite3.Connection, run_id: str
    ) -> dict[str, object] | None:
        row = connection.execute(
            """
            SELECT s.*, r.status, r.attempt, r.lease_expires_at,
                   r.task_contract_json, r.task_result_json,
                   p.branch, p.pull_request_url
            FROM agent_sessions AS s
            JOIN runs AS r ON r.run_id = s.run_id
            JOIN pbis AS p
              ON p.project_id = r.project_id
             AND p.repository_name = r.repository_name
             AND p.number = r.pbi_number
            WHERE s.run_id = ?
            """,
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        events = connection.execute(
            """
            SELECT sequence, kind, source_type, role, text, timestamp
            FROM agent_session_events WHERE run_id = ? ORDER BY sequence
            """,
            (run_id,),
        ).fetchall()
        return {
            "session_id": str(row["run_id"]),
            "worker_id": str(row["worker_id"]),
            "task": str(row["task"]),
            "state": str(row["status"]),
            "attempt": int(row["attempt"]),
            "lease_expires_at": row["lease_expires_at"],
            "branch": row["branch"],
            "pull_request_url": row["pull_request_url"],
            "task_contract": _json_mapping_or_none(row["task_contract_json"]),
            "task_result": _json_mapping_or_none(row["task_result_json"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
            "events": [
                {
                    "sequence": int(event["sequence"]),
                    "kind": str(event["kind"]),
                    "source_type": str(event["source_type"]),
                    "role": event["role"],
                    "text": str(event["text"]),
                    "timestamp": str(event["timestamp"]),
                }
                for event in events
            ],
        }

    def active_runs_for_project(self, project_id: str) -> tuple[RunState, ...]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT run_id FROM runs WHERE project_id = ? AND status = 'active'",
                (project_id,),
            ).fetchall()
            return tuple(
                run
                for row in rows
                if (run := self._run_for_id(self._connection, str(row["run_id"])))
                is not None
            )

    def is_active_repository(self, project_id: str, repository: str) -> bool:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT 1
                FROM repositories
                WHERE project_id = ? AND name = ? AND active = 1
                """,
                (project_id, repository),
            ).fetchone()
            return row is not None

    def project_state(
        self,
        project_id: str,
        event_limit: int = DEFAULT_EVENT_LIMIT,
    ) -> dict[str, object]:
        if not 1 <= event_limit <= MAX_EVENT_LIMIT:
            raise StoreError(f"event_limit must be between 1 and {MAX_EVENT_LIMIT}")
        with self._lock:
            project = self._connection.execute(
                """
                SELECT project_id, name, updated_at
                FROM projects
                WHERE project_id = ?
                """,
                (project_id,),
            ).fetchone()
            if project is None:
                raise StoreError(f"Unknown project: {project_id}")
            events_by_pbi = self._events_for_project(project_id, event_limit)
            repositories: list[dict[str, object]] = []
            repository_rows = self._connection.execute(
                """
                SELECT name, active
                FROM repositories
                WHERE project_id = ?
                ORDER BY name
                """,
                (project_id,),
            ).fetchall()
            for repository_row in repository_rows:
                repository = str(repository_row["name"])
                writer_row = (
                    self._connection.execute(
                        """
                        SELECT run_id, pbi_number, owner_id, lease_expires_at,
                               updated_at
                        FROM runs
                        WHERE project_id = ?
                          AND repository_name = ?
                          AND status = 'active'
                        """,
                        (project_id, repository),
                    ).fetchone()
                    if repository_row["active"]
                    else None
                )
                writer: dict[str, object] | None = None
                if writer_row is not None:
                    writer = {
                        "run_id": writer_row["run_id"],
                        "pbi_number": writer_row["pbi_number"],
                        "owner_id": writer_row["owner_id"],
                        "lease_expires_at": writer_row["lease_expires_at"],
                        "updated_at": writer_row["updated_at"],
                    }
                pbis: list[dict[str, object]] = []
                pbi_rows = self._connection.execute(
                    """
                    SELECT p.*, r.status, r.attempt, r.last_result AS run_result,
                           r.task_contract_json, r.task_result_json, r.task_answer
                    FROM pbis AS p
                    LEFT JOIN runs AS r
                      ON r.project_id = p.project_id
                     AND r.repository_name = p.repository_name
                     AND r.pbi_number = p.number
                    WHERE p.project_id = ? AND p.repository_name = ?
                    ORDER BY p.number
                    """,
                    (project_id, repository),
                ).fetchall()
                for pbi_row in pbi_rows:
                    events = events_by_pbi.get((repository, int(pbi_row["number"])), [])
                    pbi: dict[str, object] = {
                        "id": f"{repository}#{pbi_row['number']}",
                        "number": pbi_row["number"],
                        "title": pbi_row["title"],
                        "stage": pbi_row["stage"],
                        "run_id": pbi_row["run_id"],
                        "metadata": _json_mapping(pbi_row["metadata_json"]),
                        "status": pbi_row["status"],
                        "attempt": pbi_row["attempt"],
                        "branch": pbi_row["branch"],
                        "pull_request_url": pbi_row["pull_request_url"],
                        "last_error": pbi_row["last_error"],
                        "result": pbi_row["run_result"],
                        "task_contract": _json_mapping_or_none(
                            pbi_row["task_contract_json"]
                        ),
                        "task_result": _json_mapping_or_none(
                            pbi_row["task_result_json"]
                        ),
                        "task_answer": pbi_row["task_answer"],
                        "active": bool(pbi_row["active"]),
                        "archived": bool(pbi_row["archived"]),
                        "planning_status": pbi_row["planning_status"],
                        "claimable": bool(pbi_row["claimable"]),
                        "events": events,
                    }
                    agent_session = (
                        self._agent_session_for_run(
                            self._connection, str(pbi_row["run_id"])
                        )
                        if pbi_row["run_id"]
                        else None
                    )
                    if agent_session is not None:
                        pbi["agent_session"] = agent_session
                    pbis.append(pbi)
                repositories.append(
                    {
                        "name": repository,
                        "active": bool(repository_row["active"]),
                        "writer": writer,
                        "pbis": pbis,
                    }
                )
            return {
                "project_id": project["project_id"],
                "name": project["name"],
                "updated_at": project["updated_at"],
                "event_limit": event_limit,
                "repositories": repositories,
            }

    @staticmethod
    def _action_from_row(row: sqlite3.Row) -> dict[str, object]:
        request = json.loads(str(row["request_json"]))
        result = (
            json.loads(str(row["result_json"]))
            if row["result_json"] is not None
            else None
        )
        return {
            "id": row["action_id"],
            "project_id": row["project_id"],
            "kind": row["kind"],
            "status": row["status"],
            "repository": row["repository_name"],
            "pbi_number": row["pbi_number"],
            "run_id": row["run_id"],
            "request": request,
            "result": result,
            "error": row["error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _meta_review_run_from_row(row: sqlite3.Row) -> dict[str, object]:
        return {
            "review_id": row["review_id"],
            "project_id": row["project_id"],
            "status": row["status"],
            "record_limit": row["record_limit"],
            "input_token_limit": row["input_token_limit"],
            "selected_records": row["selected_records"],
            "input_tokens": row["input_tokens"],
            "missing_evidence": _json_list(row["missing_evidence_json"]),
            "error": row["error"],
            "created_at": row["created_at"],
            "completed_at": row["completed_at"],
        }

    @staticmethod
    def _meta_review_suggestion_from_row(row: sqlite3.Row) -> dict[str, object]:
        return {
            "suggestion_id": row["suggestion_id"],
            "project_id": row["project_id"],
            "suggestion_key": row["suggestion_key"],
            "proposed_outcome": row["proposed_outcome"],
            "rationale": row["rationale"],
            "evidence_refs": _json_list(row["evidence_refs_json"]),
            "status": row["status"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "last_review_id": row["last_review_id"],
        }

    def _run_for_id(
        self, connection: sqlite3.Connection, run_id: str
    ) -> RunState | None:
        row = connection.execute(
            """
            SELECT p.project_id, p.repository_name, p.number, p.title, p.stage,
                   p.branch, p.pull_request_url, p.last_error,
                   r.run_id, r.status, r.attempt, r.owner_id, r.lease_token,
                   r.lease_expires_at, r.last_error AS run_error,
                   r.last_result AS run_result, r.task_contract_json,
                   r.task_result_json, r.task_answer
            FROM runs AS r
            JOIN pbis AS p
              ON p.project_id = r.project_id
             AND p.repository_name = r.repository_name
             AND p.number = r.pbi_number
            WHERE r.run_id = ?
            """,
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        return self._run_from_row(row)

    @staticmethod
    def _run_from_row(row: sqlite3.Row) -> RunState:
        return RunState(
            run_id=str(row["run_id"]),
            project_id=str(row["project_id"]),
            repository=str(row["repository_name"]),
            pbi_number=int(row["number"]),
            title=str(row["title"]),
            stage=Stage(str(row["stage"])),
            status=RunStatus(str(row["status"])),
            attempt=int(row["attempt"]),
            branch=row["branch"],
            pull_request_url=row["pull_request_url"],
            last_error=row["run_error"] or row["last_error"],
            owner_id=row["owner_id"],
            lease_token=row["lease_token"],
            lease_expires_at=row["lease_expires_at"],
            last_result=row["run_result"],
            task_contract=_json_mapping_or_none(row["task_contract_json"]),
            task_result=_json_mapping_or_none(row["task_result_json"]),
            task_answer=row["task_answer"],
        )

    def _events_for_project(
        self, project_id: str, event_limit: int
    ) -> dict[tuple[str, int], list[dict[str, object]]]:
        rows = self._connection.execute(
            """
            SELECT event_id, repository_name, pbi_number, run_id, event_type,
                   from_stage, to_stage, details_json, created_at
            FROM (
                SELECT event_id, repository_name, pbi_number, run_id,
                       event_type, from_stage, to_stage, details_json,
                       created_at,
                       ROW_NUMBER() OVER (
                           PARTITION BY repository_name, pbi_number
                           ORDER BY event_id DESC
                       ) AS event_rank
                FROM events
                WHERE project_id = ?
            )
            WHERE event_rank <= ?
            ORDER BY repository_name, pbi_number, event_id
            """,
            (project_id, event_limit),
        ).fetchall()
        events_by_pbi: dict[tuple[str, int], list[dict[str, object]]] = {}
        for row in rows:
            events_by_pbi.setdefault(
                (str(row["repository_name"]), int(row["pbi_number"])), []
            ).append(
                {
                    "id": row["event_id"],
                    "run_id": row["run_id"],
                    "type": row["event_type"],
                    "from_stage": row["from_stage"],
                    "to_stage": row["to_stage"],
                    "details": json.loads(row["details_json"]),
                    "created_at": row["created_at"],
                }
            )
        return events_by_pbi

    @staticmethod
    def _record_event(
        connection: sqlite3.Connection,
        project_id: str,
        repository: str,
        number: int,
        run_id: str | None,
        event_type: str,
        from_stage: Stage,
        to_stage: Stage,
        details: dict[str, object],
    ) -> None:
        connection.execute(
            """
            INSERT INTO events(
                project_id, repository_name, pbi_number, run_id, event_type,
                from_stage, to_stage, details_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project_id,
                repository,
                number,
                run_id,
                event_type,
                from_stage.value,
                to_stage.value,
                json.dumps(details, sort_keys=True),
                _now(),
            ),
        )
