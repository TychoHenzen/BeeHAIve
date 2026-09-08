"""SQLite-backed state for durable project orchestration."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from uuid import uuid4

from .models import (
    HandoffIntent,
    ProjectSnapshot,
    RunState,
    RunStatus,
    Stage,
)


class StoreError(RuntimeError):
    """Raised when persisted orchestration state cannot satisfy an operation."""


_STAGE_ORDER = {
    Stage.BACKLOG: 0,
    Stage.REFINE: 1,
    Stage.IMPLEMENT: 2,
    Stage.PULL_REQUEST: 3,
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


class OrchestratorStore:
    """Thread-safe SQLite store with one active writer index per repository."""

    def __init__(self, database: str | Path = ":memory:") -> None:
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
                    run_id TEXT,
                    branch TEXT,
                    pull_request_url TEXT,
                    last_error TEXT,
                    handoff_base_branch TEXT,
                    handoff_body TEXT,
                    handoff_status TEXT,
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
                    last_error TEXT,
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
            for column in (
                "handoff_base_branch",
                "handoff_body",
                "handoff_status",
            ):
                if column not in pbi_columns:
                    self._connection.execute(
                        f"ALTER TABLE pbis ADD COLUMN {column} TEXT"
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

    def sync_project(self, snapshot: ProjectSnapshot) -> None:
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
                "UPDATE pbis SET active = 0 WHERE project_id = ?",
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
                    existing = connection.execute(
                        """
                        SELECT stage, run_id
                        FROM pbis
                        WHERE project_id = ? AND repository_name = ? AND number = ?
                        """,
                        (snapshot.project_id, pbi.repository, pbi.number),
                    ).fetchone()
                    if existing is None:
                        connection.execute(
                            """
                            INSERT INTO pbis(
                                project_id, repository_name, number, title,
                                stage, active
                            )
                            VALUES (?, ?, ?, ?, ?, 1)
                            """,
                            (
                                snapshot.project_id,
                                pbi.repository,
                                pbi.number,
                                pbi.title,
                                pbi.stage.value,
                            ),
                        )
                        continue

                    current_stage = Stage(str(existing["stage"]))
                    merged_stage = max(
                        (current_stage, pbi.stage),
                        key=lambda stage: _STAGE_ORDER[stage],
                    )
                    connection.execute(
                        """
                        UPDATE pbis
                        SET title = ?, stage = ?, active = 1,
                            last_error = CASE
                                WHEN ? = ? THEN last_error
                                ELSE NULL
                            END
                        WHERE project_id = ? AND repository_name = ? AND number = ?
                        """,
                        (
                            pbi.title,
                            merged_stage.value,
                            current_stage.value,
                            merged_stage.value,
                            snapshot.project_id,
                            pbi.repository,
                            pbi.number,
                        ),
                    )
                    if merged_stage is Stage.PULL_REQUEST:
                        connection.execute(
                            """
                            UPDATE runs
                            SET status = 'completed', last_error = NULL, updated_at = ?
                            WHERE project_id = ? AND repository_name = ?
                              AND pbi_number = ? AND status != 'completed'
                            """,
                            (
                                _now(),
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
                            {"source_stage": pbi.stage.value},
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
                connection.execute(
                    """
                    UPDATE runs
                    SET status = 'failed', last_error = ?, updated_at = ?
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

    def claim_next(self, project_id: str, repository: str) -> RunState | None:
        with self._transaction() as connection:
            active = connection.execute(
                """
                SELECT p.*, r.run_id, r.status, r.attempt, r.last_error AS run_error
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
                WHERE p.project_id = ? AND p.repository_name = ? AND r.status = 'active'
                LIMIT 1
                """,
                (project_id, repository),
            ).fetchone()
            if active is not None:
                return self._run_from_row(active)

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
            if isinstance(run_id, str):
                connection.execute(
                    """
                    UPDATE runs
                    SET status = 'active', attempt = attempt + 1,
                        last_error = NULL, updated_at = ?
                    WHERE run_id = ?
                    """,
                    (_now(), run_id),
                )
                event_type = "resumed"
            else:
                run_id = str(uuid4())
                connection.execute(
                    """
                    INSERT INTO runs(
                        run_id, project_id, repository_name, pbi_number,
                        status, attempt, updated_at
                    ) VALUES (?, ?, ?, ?, 'active', 1, ?)
                    """,
                    (run_id, project_id, repository, candidate["number"], _now()),
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
            return self._run_for_id(connection, run_id)

    def advance(self, run_id: str, target: Stage) -> RunState:
        with self._transaction() as connection:
            row = self._run_for_id(connection, run_id)
            if row is None:
                raise StoreError(f"Unknown run: {run_id}")
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
            connection.execute(
                "UPDATE runs SET updated_at = ? WHERE run_id = ?", (_now(), run_id)
            )
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
    ) -> RunState:
        if not branch.strip() or not pull_request_url.strip():
            raise StoreError("A branch and pull-request URL are required")
        with self._transaction() as connection:
            row = self._run_for_id(connection, run_id)
            if row is None:
                raise StoreError(f"Unknown run: {run_id}")
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
                    handoff_status = 'completed', last_error = NULL
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
                UPDATE runs SET status = 'completed', last_error = NULL, updated_at = ?
                WHERE run_id = ?
                """,
                (_now(), run_id),
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
    ) -> HandoffIntent:
        if not branch.strip():
            raise StoreError("A branch is required")
        with self._transaction() as connection:
            row = self._run_for_id(connection, run_id)
            if row is None:
                raise StoreError(f"Unknown run: {run_id}")
            persisted = connection.execute(
                """
                SELECT branch, handoff_base_branch, handoff_body, handoff_status
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
                ):
                    raise StoreError(
                        "Handoff request does not match the persisted intent"
                    )
                return HandoffIntent(
                    row,
                    str(persisted["branch"]),
                    persisted["handoff_base_branch"],
                    str(persisted["handoff_body"]),
                )
            if persisted["handoff_status"] == "completed":
                raise StoreError("Handoff intent is already completed")
            connection.execute(
                """
                UPDATE pbis
                SET branch = ?, handoff_base_branch = ?, handoff_body = ?,
                    handoff_status = 'pending'
                WHERE project_id = ? AND repository_name = ? AND number = ?
                """,
                (
                    branch,
                    base_branch,
                    body,
                    row.project_id,
                    row.repository,
                    row.pbi_number,
                ),
            )
            persisted_row = self._run_for_id(connection, run_id)
            if persisted_row is None:
                raise StoreError(f"Unknown run: {run_id}")
            return HandoffIntent(persisted_row, branch, base_branch, body)

    def pending_handoff(self, run_id: str) -> HandoffIntent | None:
        with self._lock:
            run = self._run_for_id(self._connection, run_id)
            if run is None:
                return None
            row = self._connection.execute(
                """
                SELECT branch, handoff_base_branch, handoff_body, handoff_status
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
            )

    def fail(self, run_id: str, error: str) -> RunState:
        if not error.strip():
            raise StoreError("A failure reason is required")
        with self._transaction() as connection:
            row = self._run_for_id(connection, run_id)
            if row is None:
                raise StoreError(f"Unknown run: {run_id}")
            if row.status is RunStatus.COMPLETED:
                raise StoreError("A completed run cannot fail")
            if row.status is RunStatus.FAILED:
                return row
            connection.execute(
                """
                UPDATE runs
                SET status = 'failed', last_error = ?, updated_at = ?
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
            return self._run_for_id(connection, run_id) or row

    def get_run(self, run_id: str) -> RunState | None:
        with self._lock:
            return self._run_for_id(self._connection, run_id)

    def project_state(self, project_id: str) -> dict[str, object]:
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
                        SELECT run_id, pbi_number, updated_at
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
                        "updated_at": writer_row["updated_at"],
                    }
                pbis: list[dict[str, object]] = []
                pbi_rows = self._connection.execute(
                    """
                    SELECT p.*, r.status, r.attempt
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
                    events = self._events_for_pbi(
                        project_id,
                        repository,
                        int(pbi_row["number"]),
                    )
                    pbis.append(
                        {
                            "id": f"{repository}#{pbi_row['number']}",
                            "number": pbi_row["number"],
                            "title": pbi_row["title"],
                            "stage": pbi_row["stage"],
                            "status": pbi_row["status"],
                            "attempt": pbi_row["attempt"],
                            "branch": pbi_row["branch"],
                            "pull_request_url": pbi_row["pull_request_url"],
                            "last_error": pbi_row["last_error"],
                            "active": bool(pbi_row["active"]),
                            "events": events,
                        }
                    )
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
                "repositories": repositories,
            }

    def _run_for_id(
        self, connection: sqlite3.Connection, run_id: str
    ) -> RunState | None:
        row = connection.execute(
            """
            SELECT p.project_id, p.repository_name, p.number, p.title, p.stage,
                   p.branch, p.pull_request_url, p.last_error,
                   r.run_id, r.status, r.attempt, r.last_error AS run_error
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
        )

    def _events_for_pbi(
        self, project_id: str, repository: str, number: int
    ) -> list[dict[str, object]]:
        rows = self._connection.execute(
            """
            SELECT event_id, run_id, event_type, from_stage, to_stage,
                   details_json, created_at
            FROM events
            WHERE project_id = ? AND repository_name = ? AND pbi_number = ?
            ORDER BY event_id
            """,
            (project_id, repository, number),
        ).fetchall()
        return [
            {
                "id": row["event_id"],
                "run_id": row["run_id"],
                "type": row["event_type"],
                "from_stage": row["from_stage"],
                "to_stage": row["to_stage"],
                "details": json.loads(row["details_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

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
