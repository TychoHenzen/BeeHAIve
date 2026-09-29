from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from .models import ProjectSnapshot


@dataclass(frozen=True)
class HttpCacheEntry:
    etag: str | None
    body: str
    headers: dict[str, str]


Migration = Callable[[sqlite3.Connection], None]


def _migration_two(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS workflows (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS workflow_revisions (
            workflow_id INTEGER NOT NULL,
            revision INTEGER NOT NULL,
            source_prompt TEXT NOT NULL,
            definition_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (workflow_id, revision),
            FOREIGN KEY (workflow_id) REFERENCES workflows(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS workflow_assignments (
            workflow_id INTEGER NOT NULL,
            agent_id TEXT NOT NULL,
            PRIMARY KEY (workflow_id, agent_id),
            FOREIGN KEY (workflow_id) REFERENCES workflows(id) ON DELETE CASCADE
        );
        """
    )


def _migration_one(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS http_cache (
            url TEXT PRIMARY KEY,
            etag TEXT,
            body TEXT NOT NULL,
            headers TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS project_snapshot (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            payload TEXT NOT NULL
        );
        """
    )


MIGRATIONS: tuple[Migration, ...] = (_migration_one, _migration_two)


class SnapshotDatabase:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._apply_migrations()

    def _apply_migrations(self) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"
            )
            row = self._connection.execute(
                "SELECT version FROM schema_version LIMIT 1"
            ).fetchone()
            if row is None:
                self._connection.execute("INSERT INTO schema_version VALUES (0)")
                version = 0
            else:
                version = int(row["version"])
            if version > len(MIGRATIONS):
                raise RuntimeError(f"Unsupported schema version: {version}")
            for next_version in range(version + 1, len(MIGRATIONS) + 1):
                MIGRATIONS[next_version - 1](self._connection)
                self._connection.execute(
                    "UPDATE schema_version SET version = ?", (next_version,)
                )

    def get_http_cache(self, url: str) -> HttpCacheEntry | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT etag, body, headers FROM http_cache WHERE url = ?", (url,)
            ).fetchone()
        if row is None:
            return None
        try:
            raw_headers: Any = json.loads(str(row["headers"]))
        except json.JSONDecodeError:
            raw_headers = {}
        headers: dict[str, Any] = (
            cast(dict[str, Any], raw_headers) if isinstance(raw_headers, dict) else {}
        )
        return HttpCacheEntry(
            etag=str(row["etag"]) if row["etag"] is not None else None,
            body=str(row["body"]),
            headers={str(key).lower(): str(value) for key, value in headers.items()},
        )

    def save_http_cache(
        self, url: str, etag: str | None, body: str, headers: dict[str, str]
    ) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO http_cache(url, etag, body, headers)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(url) DO UPDATE SET
                    etag = excluded.etag,
                    body = excluded.body,
                    headers = excluded.headers
                """,
                (url, etag, body, json.dumps(headers, separators=(",", ":"))),
            )

    def load_snapshot(self) -> ProjectSnapshot | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM project_snapshot WHERE id = 1"
            ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(str(row["payload"]))
        except json.JSONDecodeError as error:
            raise RuntimeError("Persisted project snapshot is invalid") from error
        return ProjectSnapshot.from_dict(payload)

    def save_snapshot(self, snapshot: ProjectSnapshot) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO project_snapshot(id, payload)
                VALUES (1, ?)
                ON CONFLICT(id) DO UPDATE SET payload = excluded.payload
                """,
                (json.dumps(snapshot.as_dict(), separators=(",", ":")),),
            )

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def transaction(self, operation: Callable[[sqlite3.Connection], Any]) -> Any:
        with self._lock, self._connection:
            return operation(self._connection)
