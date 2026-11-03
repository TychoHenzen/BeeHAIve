from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import beehaiive.app as app_module
from beehaiive.agents import AgentConflict, AgentError, AgentService
from beehaiive.app import create_app
from beehaiive.config import CoreConfig
from beehaiive.database import SnapshotDatabase
from beehaiive.snapshot import ProjectSnapshotService
from beehaiive.workflows import WorkflowStore
from tests.support import (
    BlankProvider,
    FailingProvider,
    HolderReader,
    SnapshotReader,
    config,
    held_snapshot,
    raising,
    returning,
    valid_definition,
)


@dataclass
class RoutedApp:
    app: FastAPI
    agents: AgentService


@dataclass(frozen=True)
class RouteCase:
    method: str
    path: str
    expected: int
    body: dict[str, Any] | None = None


@dataclass(frozen=True)
class AgentRouteCase:
    method: str
    path: str
    attribute: str
    behavior: Callable[..., Any]
    expected: int
    body: dict[str, Any] | None = None


ROUTE_CASES = [
    RouteCase("GET", "/", 200),
    RouteCase("GET", "/web/styles.css", 200),
    RouteCase("GET", "/api/project", 503),
    RouteCase("POST", "/api/project/refresh", 503),
    RouteCase("POST", "/api/agents", 422, {}),
    RouteCase("GET", "/api/agents/missing", 404),
    RouteCase("GET", "/api/agents/missing/logs", 404),
    RouteCase("GET", "/api/agents/missing/history", 404),
    RouteCase("PATCH", "/api/agents/missing", 404, {}),
    RouteCase("DELETE", "/api/agents/missing", 404),
    RouteCase("POST", "/api/agents/missing/start", 404, {}),
    RouteCase("POST", "/api/agents/missing/stop", 404, {}),
    RouteCase("POST", "/api/agents/missing/reset", 404, {}),
    RouteCase("POST", "/api/workflows/generate", 422, {}),
    RouteCase("GET", "/api/workflows/generate/missing", 404),
    RouteCase("POST", "/api/workflows/validate", 200, {"definition": None}),
    RouteCase("POST", "/api/workflows", 422, {"definition": {}}),
    RouteCase("GET", "/api/workflows/999", 404),
    RouteCase("POST", "/api/workflows/999/revisions", 422, {"definition": {}}),
    RouteCase("DELETE", "/api/workflows/999", 404),
]

AGENT_CASES = [
    AgentRouteCase(
        "post",
        "/api/agents",
        "create_agent",
        raising(AgentConflict("conflict")),
        409,
        {},
    ),
    AgentRouteCase(
        "patch",
        "/api/agents/agent",
        "update_agent",
        raising(AgentError("invalid")),
        422,
        {},
    ),
    AgentRouteCase(
        "post",
        "/api/agents/agent/start",
        "start_agent",
        raising(OSError("os error")),
        422,
    ),
    AgentRouteCase(
        "post",
        "/api/agents/agent/reset",
        "reset_agent",
        raising(AgentConflict("conflict")),
        409,
    ),
    AgentRouteCase(
        "delete", "/api/agents/agent", "delete_agent", returning("running"), 409
    ),
    AgentRouteCase(
        "post", "/api/agents", "create_agent", raising(OSError("os error")), 409, {}
    ),
    AgentRouteCase(
        "patch", "/api/agents/agent", "update_agent", returning(None), 404, {}
    ),
    AgentRouteCase(
        "patch", "/api/agents/agent", "update_agent", returning({}), 200, {}
    ),
    AgentRouteCase(
        "post",
        "/api/agents/agent/start",
        "start_agent",
        raising(AgentConflict("conflict")),
        409,
    ),
    AgentRouteCase(
        "post", "/api/agents/agent/reset", "reset_agent", returning(None), 404
    ),
    AgentRouteCase(
        "post", "/api/agents/agent/reset", "reset_agent", returning({}), 200
    ),
    AgentRouteCase(
        "get",
        "/api/agents/agent/history",
        "get_agent",
        returning({"history": [], "alerts": []}),
        200,
    ),
]


@pytest.fixture
def routed_app(tmp_path: Path) -> Iterator[RoutedApp]:
    config_value = CoreConfig(
        owner="TychoHenzen",
        owner_type="user",
        project_number=2,
        github_token="token",
        db_path=tmp_path / "owned.db",
    )
    database = SnapshotDatabase(":memory:")
    service = ProjectSnapshotService(
        BlankProvider(), database, minimum_refresh_seconds=0
    )
    agents = AgentService(config_value, database, WorkflowStore(database), service)
    app = create_app(
        config_value, database=database, service=service, agent_service=agents
    )
    yield RoutedApp(app, agents)
    database.close()


@pytest.fixture
def failing_client() -> Iterator[TestClient]:
    database = SnapshotDatabase(":memory:")
    service = ProjectSnapshotService(
        FailingProvider(), database, minimum_refresh_seconds=0
    )
    with TestClient(create_app(config(), database=database, service=service)) as client:
        yield client
    database.close()


@pytest.mark.parametrize(
    "case",
    ROUTE_CASES,
    ids=lambda case: f"{case.method}-{case.path}",
)
def test_failing_project_app_returns_controlled_statuses(
    case: RouteCase, failing_client: TestClient
) -> None:
    result = failing_client.request(case.method, case.path, json=case.body)
    assert result.status_code == case.expected


def test_favicon_returns_an_empty_204(failing_client: TestClient) -> None:
    favicon = failing_client.get("/favicon.ico")
    assert (favicon.status_code, favicon.content) == (204, b"")


def test_healthz_reports_ok(failing_client: TestClient) -> None:
    assert failing_client.get("/healthz").json() == {"status": "ok"}


def test_core_app_closes_owned_database(tmp_path: Path) -> None:
    owned_config = CoreConfig(
        owner="TychoHenzen",
        owner_type="user",
        project_number=2,
        github_token="token",
        db_path=tmp_path / "owned.db",
    )
    with TestClient(create_app(owned_config)) as client:
        assert client.get("/api/agents").json() == {"agents": []}


@pytest.mark.parametrize(
    "case",
    AGENT_CASES,
    ids=lambda case: f"{case.method}-{case.path}-{case.expected}",
)
def test_agent_routes_map_service_errors(
    case: AgentRouteCase, routed_app: RoutedApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(routed_app.agents, case.attribute, case.behavior)
    with TestClient(routed_app.app) as client:
        result = client.request(case.method, case.path, json=case.body)
    assert result.status_code == case.expected


def test_workflow_routes_validate_definitions(routed_app: RoutedApp) -> None:
    generate_body = {"name": "name", "prompt": "prompt", "parameters": [1]}
    definition = valid_definition()
    create_body = {"definition": definition, "name": ""}
    revise_body = {"definition": definition}
    with TestClient(routed_app.app) as client:
        generated = client.post("/api/workflows/generate", json=generate_body)
        created = client.post("/api/workflows", json=create_body)
        revised = client.post("/api/workflows/999/revisions", json=revise_body)
    assert generated.status_code == 422
    assert created.status_code == 422
    assert revised.status_code == 404


def test_workflow_routes_use_the_prepared_definition(
    routed_app: RoutedApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    accepted = SimpleNamespace(valid=True, definition={})
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
    with TestClient(routed_app.app) as client:
        created = client.post("/api/workflows", json={"definition": {}, "name": ""})
        revised = client.post("/api/workflows/999/revisions", json={"definition": {}})
    assert created.status_code == 422
    assert revised.status_code == 404


def test_read_snapshot_attaches_holders() -> None:
    payload = app_module._read_snapshot(SnapshotReader(held_snapshot()), HolderReader())
    assert payload["columns"][0]["items"][0]["holder"] == {"id": "agent"}
