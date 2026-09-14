from __future__ import annotations

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from typing import Any


class RoutingStoreCoreMixin:
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

    def _initialize(self: Any) -> None:
        with self._lock:
            self._connection.executescript(
                """
                    PRAGMA foreign_keys = ON;

                    CREATE TABLE IF NOT EXISTS routing_problems (
                        problem_id TEXT PRIMARY KEY,
                        status TEXT NOT NULL,
                        current_tier TEXT NOT NULL,
                        triage_index INTEGER NOT NULL,
                        consecutive_failures INTEGER NOT NULL,
                        bounce_count INTEGER NOT NULL,
                        round INTEGER NOT NULL,
                        total_tokens INTEGER NOT NULL,
                        total_cost REAL NOT NULL,
                        recursive_spawn_depth INTEGER NOT NULL,
                        last_failure_context TEXT,
                        required_action TEXT,
                        next_reason TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS routing_attempts (
                        attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        problem_id TEXT NOT NULL,
                        round INTEGER NOT NULL,
                        model TEXT NOT NULL,
                        tier TEXT NOT NULL,
                        reason TEXT NOT NULL,
                        outcome TEXT NOT NULL,
                        input_tokens INTEGER NOT NULL,
                        output_tokens INTEGER NOT NULL,
                        total_tokens INTEGER NOT NULL,
                        estimated_cost REAL NOT NULL,
                        bounce_count INTEGER NOT NULL,
                        recursive_spawn_depth INTEGER NOT NULL,
                        failure_context TEXT,
                        created_at TEXT NOT NULL,
                        transition_id TEXT,
                        FOREIGN KEY (problem_id) REFERENCES routing_problems(problem_id)
                    );
                    """
            )
            attempt_columns = {
                str(row["name"])
                for row in self._connection.execute(
                    "PRAGMA table_info(routing_attempts)"
                ).fetchall()
            }
            if "transition_id" not in attempt_columns:
                self._connection.execute(
                    "ALTER TABLE routing_attempts ADD COLUMN transition_id TEXT"
                )
            self._connection.execute(
                """
                    CREATE UNIQUE INDEX IF NOT EXISTS routing_attempt_transition
                    ON routing_attempts(transition_id)
                    WHERE transition_id IS NOT NULL
                    """
            )

    @contextmanager
    def _transaction(self: Any) -> Generator[sqlite3.Connection]:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
            except Exception:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()
