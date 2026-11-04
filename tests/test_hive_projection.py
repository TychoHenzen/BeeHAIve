from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from beehaiive.database import SnapshotDatabase
from beehaiive.hive import HiveProjection
from tests.hive_support import SeededHive, seed_database

FINISHED_PASS_ROW = {
    "id": "pass-1",
    "workflow_id": 1,
    "workflow_revision": 1,
    "finished_at": "2026-09-29T12:00:00+00:00",
}


@pytest.fixture
def seeded_hive(tmp_path: Path) -> Iterator[SeededHive]:
    seeded = seed_database(tmp_path)
    yield seeded
    seeded.database.close()


@pytest.fixture
def payload(seeded_hive: SeededHive) -> dict[str, Any]:
    database, _agent_id, _alert_pass_id, now = seeded_hive
    return HiveProjection(database, clock=lambda: now).snapshot()


def _write_probe(database: SnapshotDatabase) -> Any:
    return database.transaction(
        lambda connection: (
            connection.execute("SELECT version FROM schema_version").fetchone(),
            connection.execute("SELECT COUNT(*) FROM agent_alerts").fetchone(),
        )
    )


def _finished_step(index: int) -> dict[str, Any]:
    finished = datetime(2026, 9, 29, 11, 0, tzinfo=UTC) + timedelta(minutes=index)
    started = finished - timedelta(seconds=index)
    return {
        "pass_id": "pass-1",
        "state_id": "run",
        "status": "completed",
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
    }


def test_hive_snapshot_does_not_write(seeded_hive: SeededHive) -> None:
    database, _agent_id, _alert_pass_id, now = seeded_hive
    projection = HiveProjection(database, clock=lambda: now)

    before = _write_probe(database)
    projection.snapshot()
    after = _write_probe(database)

    assert before == after


def test_hive_projection_exposes_waiting_layout_and_roads(
    payload: dict[str, Any],
) -> None:
    assert payload["waiting"] == {"1": 0}
    region = payload["regions"][0]
    assert region["layout"]["run"] == {"x": 280, "y": 0}
    assert {road["outcome"] for road in region["roads"]} == {
        "always",
        "blocked",
        "done",
    }


def test_hive_projection_exposes_the_overdue_unit(
    payload: dict[str, Any], seeded_hive: SeededHive
) -> None:
    _database, agent_id, _alert_pass_id, _now = seeded_hive
    unit = payload["units"][0]
    assert unit["agent_id"] == agent_id
    assert unit["station"] == "run"
    assert unit["item"]["number"] == 231
    assert unit["typical_seconds"] == 20.0
    assert unit["overdue"] is True


def test_hive_projection_exposes_alerts(
    payload: dict[str, Any], seeded_hive: SeededHive
) -> None:
    _database, _agent_id, alert_pass_id, _now = seeded_hive
    assert payload["alerts"][0]["pass_id"] == alert_pass_id
    assert payload["alerts"][0]["station"] == "run"


def test_hive_timing_caps_finished_steps_per_state() -> None:
    database = SnapshotDatabase(":memory:")
    projection = HiveProjection(database)
    passes = cast(list[Any], [FINISHED_PASS_ROW])
    steps = cast(list[Any], [_finished_step(index) for index in range(1, 32)])
    timing = projection._timing(passes, steps, datetime.now(UTC))
    assert timing[(1, 1, "run")] == (30, 16.5)
    database.close()
