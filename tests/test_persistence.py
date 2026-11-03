from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from beehaiive.database import SnapshotDatabase
from beehaiive.models import ProjectColumn, ProjectSnapshot


def test_database_persists_schema_and_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "hive.db"
    first = SnapshotDatabase(path)
    first.save_snapshot(
        ProjectSnapshot(
            fetched_at="2026-09-29T10:00:00+00:00",
            rate_limited_until=None,
            columns=(ProjectColumn(status="Todo", items=()),),
            status_field_id="407",
            status_option_ids=(("todo", "Todo"), ("done", "Done")),
        )
    )
    first.close()
    second = SnapshotDatabase(path)
    loaded = second.load_snapshot()
    assert loaded is not None
    assert loaded.status_field_id == "407"
    assert loaded.status_option_ids == (("todo", "Todo"), ("done", "Done"))
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version FROM schema_version").fetchone() == (
            5,
        )
    second.close()


def test_database_rejects_unknown_and_corrupt_persisted_state(tmp_path: Path) -> None:
    unsupported = tmp_path / "unsupported.db"
    connection = sqlite3.connect(unsupported)
    connection.execute("CREATE TABLE schema_version(version INTEGER NOT NULL)")
    connection.execute("INSERT INTO schema_version VALUES (999)")
    connection.commit()
    connection.close()
    with pytest.raises(RuntimeError, match="Unsupported schema"):
        SnapshotDatabase(unsupported)

    database = SnapshotDatabase(":memory:")
    database.transaction(
        lambda connection: connection.execute(
            "INSERT INTO http_cache(url, etag, body, headers) VALUES (?, ?, ?, ?)",
            ("bad", None, "{}", "not-json"),
        )
    )
    cached = database.get_http_cache("bad")
    assert cached is not None
    assert cached.headers == {}
    database.transaction(
        lambda connection: connection.execute(
            "INSERT INTO project_snapshot(id, payload) VALUES (1, ?)",
            ("not-json",),
        )
    )
    with pytest.raises(RuntimeError, match="Persisted project"):
        database.load_snapshot()
    database.close()


def test_model_decoding_rejects_boolean_numbers_and_filters_values() -> None:
    snapshot = ProjectSnapshot.from_dict(
        {
            "columns": [
                {
                    "status": "Todo",
                    "items": [
                        {
                            "number": True,
                            "labels": ["good", True, {"bad": "label"}],
                            "linked_issue_numbers": ["7", True, "bad"],
                        }
                    ],
                }
            ]
        }
    )
    item = snapshot.columns[0].items[0]
    assert item.number is None
    assert item.labels == ("good", "True")
    assert item.linked_issue_numbers == (7,)
