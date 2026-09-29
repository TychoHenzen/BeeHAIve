from __future__ import annotations

import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

import pytest

from beehaiive.persistence import OrchestratorStore
from beehaiive.reviews import ReviewStore
from beehaiive.routing import RoutingStore
from beehaiive.sqlite_store_core import SQLiteStoreCoreMixin
from beehaiive.workflows import WorkflowStore


class ProbeStore(SQLiteStoreCoreMixin):
    def __init__(self, database: str | Path = ":memory:") -> None:
        SQLiteStoreCoreMixin.__init__(self, database, self._initialize)

    def _initialize(self) -> None:
        with self._lock:
            self._connection.execute("CREATE TABLE entries (value INTEGER NOT NULL)")


def test_shared_core_commits_rolls_back_and_closes() -> None:
    store = ProbeStore()
    try:
        with store._sqlite_transaction() as connection:
            connection.execute("INSERT INTO entries VALUES (1)")

        with (
            pytest.raises(RuntimeError, match="rollback"),
            store._sqlite_transaction() as connection,
        ):
            connection.execute("INSERT INTO entries VALUES (2)")
            raise RuntimeError("rollback")

        values = store._connection.execute(
            "SELECT value FROM entries ORDER BY value"
        ).fetchall()
        assert [row["value"] for row in values] == [1]
    finally:
        store.close()

    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        store._connection.execute("SELECT 1")


def test_shared_core_closes_connection_when_initialization_fails() -> None:
    class FailingStore(SQLiteStoreCoreMixin):
        connection: sqlite3.Connection | None = None

        def __init__(self) -> None:
            SQLiteStoreCoreMixin.__init__(self, ":memory:", self._fail)

        def _fail(self) -> None:
            type(self).connection = self._connection
            raise RuntimeError("initialization failed")

    with pytest.raises(RuntimeError, match="initialization failed"):
        FailingStore()

    assert FailingStore.connection is not None
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        FailingStore.connection.execute("SELECT 1")


def test_shared_core_serializes_concurrent_transactions() -> None:
    store = ProbeStore()
    try:

        def write(value: int) -> None:
            with store._sqlite_transaction() as connection:
                connection.execute("INSERT INTO entries VALUES (?)", (value,))

        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(write, range(64)))

        count = store._connection.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
        assert count == 64
    finally:
        store.close()


@pytest.mark.parametrize(
    ("factory", "transaction_name"),
    [
        (ReviewStore, "transaction"),
        (RoutingStore, "_transaction"),
        (WorkflowStore, "_transaction"),
        (OrchestratorStore, "_transaction"),
    ],
)
def test_store_transaction_names_keep_commit_and_rollback_contract(
    factory: Callable[[], Any], transaction_name: str
) -> None:
    store = factory()
    transaction: Callable[[], AbstractContextManager[sqlite3.Connection]] = getattr(
        store, transaction_name
    )
    try:
        with transaction() as connection:
            connection.execute("CREATE TABLE entries (value INTEGER NOT NULL)")
            connection.execute("INSERT INTO entries VALUES (1)")

        with (
            pytest.raises(RuntimeError, match="rollback"),
            transaction() as connection,
        ):
            connection.execute("INSERT INTO entries VALUES (2)")
            raise RuntimeError("rollback")

        values = store._connection.execute(
            "SELECT value FROM entries ORDER BY value"
        ).fetchall()
        assert [row["value"] for row in values] == [1]
    finally:
        store.close()


def test_workflow_transaction_exposes_explicit_lease_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = WorkflowStore()
    events: list[str] = []

    def reclaim(connection: sqlite3.Connection) -> None:
        assert connection.in_transaction
        events.append("reclaim")

    monkeypatch.setattr(store, "_expire_active_leases", reclaim)
    try:
        with store._transaction():
            events.append("body")
        assert events == ["reclaim", "body"]

        events.clear()
        with store._transaction(reclaim_expired=False):
            events.append("body")
        assert events == ["body"]
    finally:
        store.close()


def test_orchestrator_transaction_exposes_explicit_admission_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = OrchestratorStore()
    events: list[str] = []

    def check(connection: sqlite3.Connection) -> None:
        assert connection.in_transaction
        events.append("admission")

    monkeypatch.setattr(store, "_check_admission_configuration", check)
    try:
        with store._transaction():
            events.append("body")
        assert events == ["admission", "body"]
    finally:
        store.close()
