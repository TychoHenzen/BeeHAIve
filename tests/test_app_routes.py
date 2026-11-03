from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import beehaiive.app as app_module
from beehaiive.agents import AgentConflict, AgentError, AgentService
from beehaiive.app import create_app
from beehaiive.config import CoreConfig
from beehaiive.database import SnapshotDatabase
from beehaiive.models import ProjectCard, ProjectColumn, ProjectSnapshot
from beehaiive.project import (
    ProjectDataError,
)
from beehaiive.snapshot import ProjectSnapshotService
from beehaiive.workflows import WorkflowStore
from tests.support import config


def test_core_app_exposes_only_new_routes_and_controlled_errors(tmp_path: Path) -> None:
    class FailingProvider:
        def fetch_snapshot(self) -> ProjectSnapshot:
            raise ProjectDataError("project unavailable")

    database = SnapshotDatabase(":memory:")
    config_value = config()
    service = ProjectSnapshotService(
        FailingProvider(), database, minimum_refresh_seconds=0
    )
    app = create_app(config_value, database=database, service=service)
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        favicon = client.get("/favicon.ico")
        assert (favicon.status_code, favicon.content) == (204, b"")
        assert client.get("/healthz").json() == {"status": "ok"}
        assert client.get("/web/styles.css").status_code == 200
        assert client.get("/api/project").status_code == 503
        assert client.post("/api/project/refresh").status_code == 503
        assert client.post("/api/agents", json={}).status_code == 422
        for path in (
            "/api/agents/missing",
            "/api/agents/missing/logs",
            "/api/agents/missing/history",
        ):
            assert client.get(path).status_code == 404
        for path, method in (
            ("/api/agents/missing", "patch"),
            ("/api/agents/missing", "delete"),
            ("/api/agents/missing/start", "post"),
            ("/api/agents/missing/stop", "post"),
            ("/api/agents/missing/reset", "post"),
        ):
            response_value = (
                client.delete(path)
                if method == "delete"
                else getattr(client, method)(path, json={})
            )
            assert response_value.status_code == 404
        assert client.post("/api/workflows/generate", json={}).status_code == 422
        assert client.get("/api/workflows/generate/missing").status_code == 404
        assert (
            client.post(
                "/api/workflows/validate", json={"definition": None}
            ).status_code
            == 200
        )
        assert client.post("/api/workflows", json={"definition": {}}).status_code == 422
        assert client.get("/api/workflows/999").status_code == 404
        assert (
            client.post(
                "/api/workflows/999/revisions", json={"definition": {}}
            ).status_code
            == 422
        )
        assert client.delete("/api/workflows/999").status_code == 404
    database.close()


def test_core_app_closes_owned_database_and_maps_agent_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    owned_config = CoreConfig(
        owner="TychoHenzen",
        owner_type="user",
        project_number=2,
        github_token="token",
        db_path=tmp_path / "owned.db",
    )
    with TestClient(create_app(owned_config)) as client:
        assert client.get("/api/agents").json() == {"agents": []}

    database = SnapshotDatabase(":memory:")

    class Provider:
        def fetch_snapshot(self) -> ProjectSnapshot:
            return ProjectSnapshot(None, None, ())

    service = ProjectSnapshotService(
        Provider(),
        database,
        minimum_refresh_seconds=0,
    )
    agents = AgentService(owned_config, database, WorkflowStore(database), service)
    app = create_app(
        owned_config, database=database, service=service, agent_service=agents
    )

    def conflict(*_args: Any, **_kwargs: Any) -> Any:
        raise AgentConflict("conflict")

    def invalid(*_args: Any, **_kwargs: Any) -> Any:
        raise AgentError("invalid")

    def operating_system_error(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("os error")

    monkeypatch.setattr(agents, "create_agent", conflict)
    monkeypatch.setattr(agents, "update_agent", invalid)
    monkeypatch.setattr(agents, "start_agent", operating_system_error)
    monkeypatch.setattr(agents, "reset_agent", conflict)
    monkeypatch.setattr(agents, "delete_agent", lambda _agent_id: "running")
    with TestClient(app) as client:
        assert client.post("/api/agents", json={}).status_code == 409
        assert client.patch("/api/agents/agent", json={}).status_code == 422
        assert client.post("/api/agents/agent/start").status_code == 422
        assert client.post("/api/agents/agent/reset").status_code == 409
        assert client.delete("/api/agents/agent").status_code == 409
        monkeypatch.setattr(agents, "create_agent", operating_system_error)
        assert client.post("/api/agents", json={}).status_code == 409
        monkeypatch.setattr(agents, "update_agent", lambda *_args, **_kwargs: None)
        assert client.patch("/api/agents/agent", json={}).status_code == 404
        monkeypatch.setattr(agents, "update_agent", lambda *_args, **_kwargs: {})
        assert client.patch("/api/agents/agent", json={}).status_code == 200
        monkeypatch.setattr(agents, "start_agent", conflict)
        assert client.post("/api/agents/agent/start").status_code == 409
        monkeypatch.setattr(agents, "reset_agent", lambda *_args, **_kwargs: None)
        assert client.post("/api/agents/agent/reset").status_code == 404
        monkeypatch.setattr(agents, "reset_agent", lambda *_args, **_kwargs: {})
        assert client.post("/api/agents/agent/reset").status_code == 200
        monkeypatch.setattr(
            agents,
            "get_agent",
            lambda _agent_id: {"history": [], "alerts": []},
        )
        assert client.get("/api/agents/agent/history").status_code == 200
        assert (
            client.post(
                "/api/workflows/generate",
                json={"name": "name", "prompt": "prompt", "parameters": [1]},
            ).status_code
            == 422
        )
        valid_definition = {
            "schema_version": 1,
            "name": "Delivery",
            "description": "Wait for work.",
            "source_prompt": "Wait for backlog.",
            "auto_reset_on_stall": False,
            "max_steps_per_pass": 4,
            "parameters": [
                {"name": "status", "type": "status", "const": True, "value": "Backlog"}
            ],
            "initial": "wait",
            "states": [
                {
                    "id": "wait",
                    "title": "Wait",
                    "action": "wait_for_work",
                    "max_visits": 1,
                },
                {
                    "id": "escalate",
                    "title": "Escalate",
                    "action": "escalate",
                    "max_visits": 1,
                },
            ],
            "transitions": [
                {
                    "from": "wait",
                    "to": "escalate",
                    "priority": 1,
                    "conditions": [{"kind": "item_status_is", "value": "{status}"}],
                },
                {
                    "from": "escalate",
                    "to": "wait",
                    "priority": 1,
                    "conditions": [{"kind": "always"}],
                },
            ],
        }
        assert (
            client.post(
                "/api/workflows",
                json={"definition": valid_definition, "name": ""},
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/workflows/999/revisions",
                json={"definition": valid_definition},
            ).status_code
            == 404
        )
        accepted = type("Accepted", (), {"valid": True, "definition": {}})()
        monkeypatch.setattr(
            app_module,
            "validate_workflow",
            lambda *_args, **_kwargs: accepted,
        )
        monkeypatch.setattr(
            app_module,
            "assign_layered_layout",
            lambda _definition: {"name": "fallback", "source_prompt": "prompt"},
        )
        assert (
            client.post(
                "/api/workflows",
                json={"definition": {}, "name": ""},
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/workflows/999/revisions",
                json={"definition": {}},
            ).status_code
            == 404
        )

    held_card = ProjectCard(
        type="Issue",
        repository="acme/repo",
        number=1,
        title="held",
        url=None,
        state="open",
        labels=(),
        linked_issue_numbers=(),
        item_key="held",
    )
    held_snapshot = ProjectSnapshot(
        fetched_at="2026-09-29T10:00:00+00:00",
        rate_limited_until=None,
        columns=(ProjectColumn("Todo", (held_card,)),),
    )
    holder_payload = app_module._read_snapshot(
        type("SnapshotReader", (), {"get_snapshot": lambda _self: held_snapshot})(),
        type(
            "HolderReader",
            (),
            {"holders": lambda _self: {"held": {"id": "agent"}}},
        )(),
    )
    assert holder_payload["columns"][0]["items"][0]["holder"] == {"id": "agent"}
    database.close()
