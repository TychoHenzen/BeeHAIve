from __future__ import annotations

import sqlite3
from typing import Any

from beehaiive.contract_types.validation import _redact_text
from beehaiive.models import RunState, RunStatus

from .errors import StoreError
from .helpers.lease_helpers import _now


class AdmissionError(StoreError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def repository_identity(repository: str) -> str:
    return repository.strip().casefold().rstrip("/").removesuffix(".git")


class AdmissionMixin:
    @property
    def admission_enabled(self: Any) -> bool:
        return self._admission_capacity is not None

    def _initialize_admission(self: Any) -> None:
        self._connection.executescript("""
            CREATE TABLE IF NOT EXISTS admission_config (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                capacity INTEGER NOT NULL CHECK (capacity > 0)
            );
            CREATE TABLE IF NOT EXISTS repository_generations (
                repository TEXT PRIMARY KEY,
                generation INTEGER NOT NULL CHECK (generation > 0)
            );
            CREATE TABLE IF NOT EXISTS admissions (
                repository TEXT PRIMARY KEY,
                run_id TEXT NOT NULL UNIQUE,
                owner_id TEXT NOT NULL,
                lease_token TEXT NOT NULL,
                generation INTEGER NOT NULL,
                expires_at TEXT NOT NULL
            );
            CREATE TRIGGER IF NOT EXISTS release_run_admission
            AFTER UPDATE OF status, lease_token ON runs
            WHEN NEW.status != 'active' OR NEW.lease_token IS NULL
            BEGIN
                DELETE FROM admissions
                WHERE run_id = OLD.run_id AND lease_token = OLD.lease_token;
            END;
            CREATE TRIGGER IF NOT EXISTS delete_run_admission
            AFTER DELETE ON runs
            BEGIN
                DELETE FROM admissions
                WHERE run_id = OLD.run_id AND lease_token = OLD.lease_token;
            END;
        """)
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                columns = {
                    row["name"]
                    for row in self._connection.execute("PRAGMA table_info(runs)")
                }
                if "admission_generation" not in columns:
                    self._connection.execute(
                        "ALTER TABLE runs ADD COLUMN admission_generation INTEGER"
                    )
                configured = self._connection.execute(
                    "SELECT capacity FROM admission_config WHERE singleton = 1"
                ).fetchone()
                if configured is None and self._admission_capacity is not None:
                    self._connection.execute(
                        "INSERT INTO admission_config VALUES (1, ?)",
                        (self._admission_capacity,),
                    )
                    # Account for every active run, including expired legacy leases.
                    rows = self._connection.execute(
                        "SELECT run_id, repository_name FROM runs "
                        "WHERE status = 'active'"
                    ).fetchall()
                    identities = {
                        repository_identity(r["repository_name"]) for r in rows
                    }
                    if len(rows) > self._admission_capacity or len(identities) != len(
                        rows
                    ):
                        raise AdmissionError("admission_activation_conflict")
                    for row in rows:
                        run = self._run_for_id(self._connection, row["run_id"])
                        self._admit(self._connection, run, reclaim=False)
                self._check_admission_configuration(self._connection)
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

    def _check_admission_configuration(
        self: Any, connection: sqlite3.Connection
    ) -> None:
        configured = connection.execute(
            "SELECT capacity FROM admission_config WHERE singleton = 1"
        ).fetchone()
        capacity = None if configured is None else int(configured["capacity"])
        if capacity != self._admission_capacity:
            raise AdmissionError("admission_authority_unavailable")

    def _admit(
        self: Any,
        connection: sqlite3.Connection,
        run: RunState,
        *,
        reclaim: bool = True,
    ) -> None:
        if self._admission_capacity is None:
            return
        if not run.owner_id or not run.lease_token or not run.lease_expires_at:
            raise AdmissionError("admission_activation_conflict")
        if reclaim:
            connection.execute(
                "DELETE FROM admissions WHERE expires_at <= ?", (_now(),)
            )
        identity = repository_identity(run.repository)
        occupied = connection.execute(
            "SELECT 1 FROM admissions WHERE repository = ?", (identity,)
        ).fetchone()
        if occupied is not None:
            raise AdmissionError("repository_owned")
        used = connection.execute("SELECT COUNT(*) FROM admissions").fetchone()[0]
        if used >= self._admission_capacity:
            raise AdmissionError("capacity_exhausted")
        generation = connection.execute(
            """INSERT INTO repository_generations VALUES (?, 1)
               ON CONFLICT(repository) DO UPDATE SET generation = generation + 1
               RETURNING generation""",
            (identity,),
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO admissions VALUES (?, ?, ?, ?, ?, ?)",
            (
                identity,
                run.run_id,
                run.owner_id,
                run.lease_token,
                generation,
                run.lease_expires_at,
            ),
        )
        connection.execute(
            "UPDATE runs SET admission_generation = ? WHERE run_id = ?",
            (generation, run.run_id),
        )

    def _require_admission(self: Any, run: RunState) -> None:
        self._check_admission_configuration(self._connection)
        if self._admission_capacity is None:
            return
        current = self._connection.execute(
            """SELECT a.generation FROM admissions a
               JOIN repository_generations g
                 ON g.repository = a.repository AND g.generation = a.generation
               WHERE a.run_id = ? AND a.repository = ? AND a.owner_id = ?
                 AND a.lease_token = ? AND a.expires_at > ?""",
            (
                run.run_id,
                repository_identity(run.repository),
                run.owner_id,
                run.lease_token,
                _now(),
            ),
        ).fetchone()
        if (
            run.status is not RunStatus.ACTIVE
            or current is None
            or current["generation"] != run.admission_generation
        ):
            raise StoreError("Run admission is no longer valid")

    def admission_state(self: Any, project_id: str) -> dict[str, object]:
        with self._transaction() as connection:
            if self._admission_capacity is None:
                return {"enabled": False}
            now = _now()
            used = connection.execute(
                "SELECT COUNT(*) FROM admissions WHERE expires_at > ?", (now,)
            ).fetchone()[0]
            rows = connection.execute(
                """SELECT a.repository, a.run_id, a.owner_id,
                          a.generation, a.expires_at
                   FROM admissions a JOIN runs r ON r.run_id = a.run_id
                   WHERE r.project_id = ? AND a.expires_at > ?
                   ORDER BY a.repository LIMIT 100""",
                (project_id, now),
            ).fetchall()
            return {
                "enabled": True,
                "capacity": self._admission_capacity,
                "used": used,
                "free": self._admission_capacity - used,
                "reservations": [
                    {
                        **dict(row),
                        "owner_id": _redact_text(str(row["owner_id"]), 200),
                    }
                    for row in rows
                ],
            }
