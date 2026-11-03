from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from beehaiive import services
from beehaiive.config import CoreConfig
from beehaiive.database import SnapshotDatabase
from beehaiive.services import build_services


class ConstructorFailure(Exception):
    pass


def failing_workflow_store(database: SnapshotDatabase) -> None:
    raise ConstructorFailure("workflow store failed")


def owned_config(tmp_path: Path) -> CoreConfig:
    return CoreConfig(
        owner="TychoHenzen",
        owner_type="user",
        project_number=2,
        github_token="token",
        db_path=tmp_path / "owned.db",
    )


def test_build_services_closes_owned_database_when_a_constructor_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[sqlite3.Connection] = []
    real_connect = sqlite3.connect

    def spy(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        connection = real_connect(*args, **kwargs)
        opened.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", spy)
    monkeypatch.setattr(services, "WorkflowStore", failing_workflow_store)
    with pytest.raises(ConstructorFailure, match="workflow store failed"):
        build_services(owned_config(tmp_path), {})
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened[0].execute("SELECT 1")


def test_build_services_keeps_supplied_database_open_when_a_constructor_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = SnapshotDatabase(":memory:")
    monkeypatch.setattr(services, "WorkflowStore", failing_workflow_store)
    with pytest.raises(ConstructorFailure):
        build_services(owned_config(tmp_path), {"database": database})
    assert database.load_snapshot() is None
    database.close()
