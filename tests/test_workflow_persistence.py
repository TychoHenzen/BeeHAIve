from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from beehaiive.app import create_app
from beehaiive.database import SnapshotDatabase
from beehaiive.snapshot import ProjectSnapshotService
from beehaiive.workflows import WorkflowStore, assign_layered_layout
from tests.support import skills_config
from tests.workflow_support import EmptyProvider, definition, write_skill


def workflow_app(tmp_path: Path, database: SnapshotDatabase) -> FastAPI:
    service = ProjectSnapshotService(
        EmptyProvider(), database, minimum_refresh_seconds=0
    )
    write_skill(tmp_path)
    return create_app(skills_config(tmp_path), database=database, service=service)


def save_two_revisions(client: TestClient) -> None:
    first = {"source_prompt": "first", "definition": definition()}
    revised_definition = definition()
    revised_definition["description"] = "Second revision"
    second = {"source_prompt": "second", "definition": revised_definition}
    client.post("/api/workflows", json=first)
    client.post("/api/workflows/1/revisions", json=second)


def assign_agent(database: SnapshotDatabase) -> None:
    database.transaction(
        lambda connection: connection.execute(
            "INSERT INTO workflow_assignments(workflow_id, agent_id) "
            "VALUES (1, 'agent')"
        )
    )


def clear_assignments(database: SnapshotDatabase) -> None:
    database.transaction(
        lambda connection: connection.execute("DELETE FROM workflow_assignments")
    )


def test_layered_layout_only_fills_missing_positions() -> None:
    value = definition()
    value["states"][0]["layout"] = {"x": 99, "y": 12}
    laid_out = assign_layered_layout(value)
    assert laid_out["states"][0]["layout"] == {"x": 99.0, "y": 12.0}
    assert all("layout" in state for state in laid_out["states"])


def test_workflow_store_versions_revisions_and_assignment_deletion(
    tmp_path: Path,
) -> None:
    database = SnapshotDatabase(tmp_path / "hive.db")
    store = WorkflowStore(database)
    created = store.create_workflow("Delivery", "prompt", definition())
    assert created["latest"]["revision"] == 1
    revised = store.add_revision(1, "second", definition())
    assert revised is not None
    assert revised["latest"]["revision"] == 2
    assert len(revised["revisions"]) == 2

    assign_agent(database)
    assert store.delete_workflow(1) == "assigned"
    clear_assignments(database)
    assert store.delete_workflow(1) == "deleted"
    database.close()


def test_workflow_store_reports_missing_rows_and_corrupt_revisions() -> None:
    database = SnapshotDatabase(":memory:")
    store = WorkflowStore(database)
    assert store.list_workflows() == []
    assert store.get_workflow(999) is None
    assert store.add_revision(999, "missing", definition()) is None
    database.transaction(
        lambda connection: connection.execute(
            "INSERT INTO workflows(id, name, created_at) VALUES (99, 'bad', 'now')"
        )
    )
    database.transaction(
        lambda connection: connection.execute(
            "INSERT INTO workflow_revisions(workflow_id, revision, source_prompt, "
            "definition_json, created_at) "
            "VALUES (99, 1, 'prompt', 'not-json', 'now')"
        )
    )
    with pytest.raises(RuntimeError, match="Persisted workflow"):
        store.get_workflow(99)
    database.close()


def test_workflow_http_api_rejects_invalid_save_and_validates_definitions(
    tmp_path: Path,
) -> None:
    database = SnapshotDatabase(tmp_path / "hive.db")
    app = workflow_app(tmp_path, database)

    with TestClient(app) as client:
        invalid = client.post("/api/workflows", json={"definition": {"name": "bad"}})
        validation = client.post(
            "/api/workflows/validate", json={"definition": definition()}
        )

    assert invalid.status_code == 422
    assert validation.status_code == 200
    assert validation.json()["valid"]
    database.close()


def test_workflow_http_api_persists_revision_history(tmp_path: Path) -> None:
    database = SnapshotDatabase(tmp_path / "hive.db")
    app = workflow_app(tmp_path, database)
    first = {"source_prompt": "first", "definition": definition()}
    revised_definition = definition()
    revised_definition["description"] = "Second revision"
    second = {"source_prompt": "second", "definition": revised_definition}

    with TestClient(app) as client:
        created = client.post("/api/workflows", json=first)
        revision = client.post("/api/workflows/1/revisions", json=second)
        listed = client.get("/api/workflows")
        detail = client.get("/api/workflows/1")

    assert created.status_code == 200
    assert revision.status_code == 200
    assert listed.json()["workflows"][0]["name"] == "Delivery"
    assert detail.json()["latest"]["revision"] == 2
    assert [item["source_prompt"] for item in detail.json()["revisions"]] == [
        "first",
        "second",
    ]
    assert (
        detail.json()["revisions"][0]["definition"]["description"]
        != (detail.json()["revisions"][1]["definition"]["description"])
    )
    database.close()


def test_workflow_http_api_reloads_and_deletes_workflows_after_reopen(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "hive.db"
    database = SnapshotDatabase(database_path)
    with TestClient(workflow_app(tmp_path, database)) as client:
        save_two_revisions(client)
    database.close()

    reopened = SnapshotDatabase(database_path)
    with TestClient(workflow_app(tmp_path, reopened)) as client:
        reopened_detail = client.get("/api/workflows/1")
        assign_agent(reopened)
        assigned_delete = client.delete("/api/workflows/1")
        clear_assignments(reopened)
        deleted = client.delete("/api/workflows/1")

    assert reopened_detail.status_code == 200
    assert reopened_detail.json()["revisions"][0]["source_prompt"] == "first"
    assert reopened_detail.json()["revisions"][1]["source_prompt"] == "second"
    assert assigned_delete.status_code == 409
    assert deleted.status_code == 200
    reopened.close()
