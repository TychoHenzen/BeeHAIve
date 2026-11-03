from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.error import URLError

from fastapi import FastAPI
from fastapi.testclient import TestClient

from beehaiive.app import create_app
from beehaiive.database import SnapshotDatabase
from beehaiive.project import (
    ProjectProvider,
)
from beehaiive.rest import (
    GithubRestClient,
    HttpResponse,
)
from beehaiive.snapshot import ProjectSnapshotService
from tests.support import (
    FIELDS_URL,
    ITEMS_URL,
    PROJECT_URL,
    REPOSITORY,
    REPOSITORY_URL,
    FakeTransport,
    config,
    expected_card,
    issue_content,
    option,
    project_item,
    project_responses,
    response,
    status_field,
    status_value,
)

NEXT_URL = f"{PROJECT_URL}/items?page=2"

PULL_REQUEST = {
    "number": 8,
    "title": "Ship board",
    "html_url": f"{REPOSITORY_URL}/pull/8",
    "state": "open",
    "body": "Closes #7 and fixes acme/other#12",
    "repository_url": f"https://api.github.com/repos/{REPOSITORY}",
    "labels": [],
}


def mixed_items() -> list[dict[str, Any]]:
    labels = [{"name": "enhancement"}]
    issue = issue_content(7, "Build board", state="open", labels=labels)
    draft = {"title": "Unassigned idea", "body": "later"}
    issue_status = status_value("todo", "Todo")
    pull_request_status = status_value("done", "Done")
    draft_status = status_value("backlog", "Backlog")
    return [
        project_item("Issue", issue, issue_status),
        project_item("PullRequest", PULL_REQUEST, pull_request_status),
        project_item("DraftIssue", draft, draft_status),
    ]


def fetch_project_payload() -> dict[str, Any]:
    options = (
        option("backlog", "Backlog", raw=True),
        option("todo", "Todo", raw=True),
        option("done", "Done", raw=True),
    )
    project = [response({"id": 25778678})]
    fields = [response([status_field(*options)])]
    items = [response(mixed_items())]
    transport = FakeTransport(
        {PROJECT_URL: project, FIELDS_URL: fields, ITEMS_URL: items}
    )
    database, provider = project_responses(config(), transport)
    service = ProjectSnapshotService(provider, database, minimum_refresh_seconds=0)
    app = create_app(config(), database=database, service=service)
    with TestClient(app) as client:
        result = client.get("/api/project")
    database.close()
    assert result.status_code == 200
    return result.json()


def test_project_api_orders_columns_by_status_options() -> None:
    payload = fetch_project_payload()

    assert [column["status"] for column in payload["columns"]] == [
        "Backlog",
        "Todo",
        "Done",
        "No status",
    ]
    assert payload["fetched_at"]
    assert payload["rate_limited_until"] is None


def test_project_api_normalizes_issue_pull_request_and_draft_items() -> None:
    columns = fetch_project_payload()["columns"]
    draft = expected_card(type="DraftIssue", title="Unassigned idea")
    issue = expected_card(
        type="Issue",
        repository=REPOSITORY,
        number=7,
        title="Build board",
        url=f"{REPOSITORY_URL}/issues/7",
        state="open",
        labels=["enhancement"],
    )
    pull_request = expected_card(
        type="PullRequest",
        repository=REPOSITORY,
        number=8,
        title="Ship board",
        url=f"{REPOSITORY_URL}/pull/8",
        state="open",
        linked_issue_numbers=[7, 12],
    )

    assert columns[0]["items"][0] == draft
    assert columns[1]["items"][0] == issue
    assert columns[2]["items"][0] == pull_request


@dataclass
class ProjectApp:
    app: FastAPI
    database: SnapshotDatabase
    transport: FakeTransport


def project_app(transport: FakeTransport, minimum_refresh_seconds: float) -> ProjectApp:
    database, provider = project_responses(config(), transport)
    service = ProjectSnapshotService(
        provider, database, minimum_refresh_seconds=minimum_refresh_seconds
    )
    app = create_app(config(), database=database, service=service)
    return ProjectApp(app, database, transport)


def test_cursor_pagination_follows_the_next_link() -> None:
    first_page = [response([], headers={"Link": f'<{NEXT_URL}>; rel="next"'})]
    second_page = [response([])]
    fields = [response([status_field(option("todo", "Todo"))])]
    transport = FakeTransport(
        {
            PROJECT_URL: [response({"id": 1})],
            FIELDS_URL: fields,
            ITEMS_URL: first_page,
            NEXT_URL: second_page,
        }
    )
    project = project_app(transport, 60)

    with TestClient(project.app) as client:
        result = client.get("/api/project")
        refresh = client.post("/api/project/refresh")
    project.database.close()

    assert result.status_code == 200
    assert refresh.status_code == 200
    assert refresh.json() == result.json()
    assert [call[0] for call in transport.calls] == [
        PROJECT_URL,
        FIELDS_URL,
        ITEMS_URL,
        NEXT_URL,
    ]


def test_dashboard_assets_and_read_only_project_routes() -> None:
    project = project_app(FakeTransport({}), 60)

    with TestClient(project.app) as client:
        page = client.get("/")
        script = client.get("/web/app.js")
    openapi = project.app.openapi()
    project.database.close()

    assert page.status_code == 200
    assert all(label in page.text for label in ("Board", "Workflows", "Agents", "Hive"))
    assert script.status_code == 200
    assert "fetch" in script.text
    assert "held by agent X" not in script.text
    assert "unclaimed" in script.text
    assert 'load("/api/project/refresh", "POST")' in script.text
    assert set(openapi["paths"]["/api/project"]) == {"get"}
    assert set(openapi["paths"]["/api/project/refresh"]) == {"post"}


def failing_transport(_url: str, _headers: Mapping[str, str]) -> HttpResponse:
    raise URLError("offline")


def test_transport_io_failure_returns_controlled_api_error() -> None:
    database = SnapshotDatabase(":memory:")

    provider = ProjectProvider(
        config(), GithubRestClient("token", database, transport=failing_transport)
    )
    app = create_app(
        config(),
        database=database,
        service=ProjectSnapshotService(provider, database, minimum_refresh_seconds=0),
    )

    with TestClient(app) as client:
        result = client.get("/api/project")

    assert result.status_code == 502
    assert "offline" in result.json()["detail"]
    database.close()
