from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from beehaiive.dashboard import build_dashboard_state
from beehaiive.models import PbiSnapshot, ProjectSnapshot, RepositorySnapshot
from beehaiive.storage import OrchestratorStore, StoreError


def test_worker_host_registration_is_bounded_idempotent_and_redacted(tmp_path) -> None:
    database = tmp_path / "hosts.sqlite3"
    store = OrchestratorStore(database)
    try:
        first = store.register_worker_host(
            "host-a",
            3,
            ["python", "codex", "python"],
            "token=host-secret",
            heartbeat_at="2026-01-01T00:00:00+00:00",
        )
        second = store.register_worker_host(
            "host-a",
            4,
            ["codex", "python"],
            "ready",
            heartbeat_at="2026-01-01T00:00:10+00:00",
        )

        assert first["host_id"] == second["host_id"] == "host-a"
        assert second["worker_slots"] == 4
        assert second["capabilities"] == ["codex", "python"]
        assert "host-secret" not in str(first)
        assert (
            store.worker_host_records(now="2026-01-01T00:00:10+00:00")[0]["liveness"]
            == "active"
        )
        assert (
            store._connection.execute("SELECT COUNT(*) FROM worker_hosts").fetchone()[0]
            == 1
        )

        store.close()
        store = OrchestratorStore(database)
        restored = store.worker_host_for_id("host-a", now="2026-01-01T00:00:20+00:00")
        assert restored["capabilities"] == ["codex", "python"]
        assert restored["worker_slots"] == 4
    finally:
        store.close()


def test_worker_host_liveness_is_store_time_based_and_fail_closed() -> None:
    store = OrchestratorStore()
    try:
        store.register_worker_host(
            "host-a",
            2,
            (),
            heartbeat_at="2026-01-01T00:00:00+00:00",
        )
        assert (
            store.worker_host_for_id(
                "host-a",
                now="2026-01-01T00:01:30+00:00",
                heartbeat_seconds=30,
                stale_seconds=90,
            )["liveness"]
            == "active"
        )
        stale = store.worker_host_for_id(
            "host-a",
            now="2026-01-01T00:01:30.001000+00:00",
            heartbeat_seconds=30,
            stale_seconds=90,
        )
        assert stale["liveness"] == "stale"
        assert stale["available_worker_slots"] == 0
        assert store.worker_host_for_id("missing")["liveness"] == "unknown"
        with pytest.raises(StoreError, match="exceed"):
            store.worker_host_records(heartbeat_seconds=30, stale_seconds=30)
    finally:
        store.close()


def test_worker_host_validation_and_concurrent_refresh() -> None:
    store = OrchestratorStore()
    try:
        with pytest.raises(StoreError, match="stable"):
            store.register_worker_host(" ", 1, ())
        with pytest.raises(StoreError, match="slots"):
            store.register_worker_host("host", 0, ())
        with pytest.raises(StoreError, match="capabilities"):
            store.register_worker_host("host", 1, "codex")
        with pytest.raises(StoreError, match="too long"):
            store.register_worker_host("host", 1, ["x" * 81])
        with pytest.raises(StoreError, match="Unknown"):
            store.heartbeat_worker_host("missing")
        store.register_worker_host("host", 1, ["codex"])
        with ThreadPoolExecutor(max_workers=4) as executor:
            list(
                executor.map(
                    lambda index: store.heartbeat_worker_host("host", str(index)),
                    range(8),
                )
            )
        assert (
            store._connection.execute("SELECT COUNT(*) FROM worker_hosts").fetchone()[0]
            == 1
        )
    finally:
        store.close()


def test_dashboard_projection_exposes_bounded_worker_hosts() -> None:
    store = OrchestratorStore()
    try:
        store.sync_project(
            ProjectSnapshot(
                "project-1",
                "Planning",
                (
                    RepositorySnapshot(
                        "owner/api", (PbiSnapshot("owner/api", 1, "PBI"),)
                    ),
                ),
            )
        )
        store.register_worker_host("host-a", 2, ["codex"])
        state = store.project_state("project-1")
        projected = build_dashboard_state(state)
        assert projected["worker_hosts"][0]["host_id"] == "host-a"
        assert projected["worker_hosts"][0]["available_worker_slots"] == 2
    finally:
        store.close()
