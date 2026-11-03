from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from beehaiive.database import SnapshotDatabase
from beehaiive.models import ProjectSnapshot
from tests.support import stored_snapshot

RAW_ITEM = {
    "number": True,
    "labels": ["good", True, {"bad": "label"}],
    "linked_issue_numbers": ["7", True, "bad"],
}
RAW_COLUMN = {"status": "Todo", "items": [RAW_ITEM]}
RAW_SNAPSHOT = {"columns": [RAW_COLUMN]}
INSERT_CORRUPT_CACHE_SQL = (
    "INSERT INTO http_cache(url, etag, body, headers) VALUES (?, ?, ?, ?)"
)
INSERT_CORRUPT_SNAPSHOT_SQL = "INSERT INTO project_snapshot(id, payload) VALUES (1, ?)"


def write_schema_version(path: Path, version: int) -> None:
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE schema_version(version INTEGER NOT NULL)")
    connection.execute("INSERT INTO schema_version VALUES (?)", (version,))
    connection.commit()
    connection.close()


def run_sql(database: SnapshotDatabase, sql: str, params: tuple[Any, ...] = ()) -> None:
    database.transaction(lambda connection: connection.execute(sql, params))


def test_database_persists_schema_and_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "hive.db"
    first = SnapshotDatabase(path)
    first.save_snapshot(stored_snapshot("Todo"))
    first.close()
    second = SnapshotDatabase(path)
    loaded = second.load_snapshot()
    assert loaded is not None
    assert loaded.status_field_id == "407"
    assert loaded.status_option_ids == (("todo", "Todo"), ("done", "Done"))
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT version FROM schema_version").fetchone() == (
            5,
        )
    second.close()


def test_database_rejects_unsupported_schema_version(tmp_path: Path) -> None:
    unsupported = tmp_path / "unsupported.db"
    write_schema_version(unsupported, 999)
    with pytest.raises(RuntimeError, match="Unsupported schema"):
        SnapshotDatabase(unsupported)


def test_database_ignores_corrupt_cached_headers() -> None:
    database = SnapshotDatabase(":memory:")
    bad_row = ("bad", None, "{}", "not-json")
    run_sql(database, INSERT_CORRUPT_CACHE_SQL, bad_row)
    cached = database.get_http_cache("bad")
    assert cached is not None
    assert cached.headers == {}
    database.close()


def test_database_rejects_a_corrupt_snapshot_payload() -> None:
    database = SnapshotDatabase(":memory:")
    bad_payload = ("not-json",)
    run_sql(database, INSERT_CORRUPT_SNAPSHOT_SQL, bad_payload)
    with pytest.raises(RuntimeError, match="Persisted project"):
        database.load_snapshot()
    database.close()


def test_model_decoding_rejects_boolean_numbers_and_filters_values() -> None:
    snapshot = ProjectSnapshot.from_dict(RAW_SNAPSHOT)
    item = snapshot.columns[0].items[0]
    assert item.number is None
    assert item.labels == ("good", "True")
    assert item.linked_issue_numbers == (7,)
