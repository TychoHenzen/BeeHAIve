from __future__ import annotations

from collections.abc import Mapping
from urllib.error import URLError

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
from tests.support import FakeTransport, config, project_responses, response


def test_project_api_normalizes_mixed_items_and_status_order() -> None:
    project_url = "https://api.github.com/users/TychoHenzen/projectsV2/2"
    fields_url = f"{project_url}/fields"
    items_url = f"{project_url}/items?per_page=100&fields=407"
    transport = FakeTransport(
        {
            project_url: [response({"id": 25778678})],
            fields_url: [
                response(
                    [
                        {
                            "id": 407,
                            "name": "Status",
                            "data_type": "single_select",
                            "options": [
                                {"id": "backlog", "name": {"raw": "Backlog"}},
                                {"id": "todo", "name": {"raw": "Todo"}},
                                {"id": "done", "name": {"raw": "Done"}},
                            ],
                        }
                    ]
                )
            ],
            items_url: [
                response(
                    [
                        {
                            "content_type": "Issue",
                            "content": {
                                "number": 7,
                                "title": "Build board",
                                "html_url": "https://github.com/TychoHenzen/BeeHAIve/issues/7",
                                "state": "open",
                                "repository": {"full_name": "TychoHenzen/BeeHAIve"},
                                "labels": [{"name": "enhancement"}],
                            },
                            "fields": [
                                {
                                    "id": 407,
                                    "value": {"id": "todo", "name": {"raw": "Todo"}},
                                }
                            ],
                        },
                        {
                            "content_type": "PullRequest",
                            "content": {
                                "number": 8,
                                "title": "Ship board",
                                "html_url": "https://github.com/TychoHenzen/BeeHAIve/pull/8",
                                "state": "open",
                                "body": "Closes #7 and fixes acme/other#12",
                                "repository_url": "https://api.github.com/repos/TychoHenzen/BeeHAIve",
                                "labels": [],
                            },
                            "fields": [
                                {
                                    "id": 407,
                                    "value": {"id": "done", "name": {"raw": "Done"}},
                                }
                            ],
                        },
                        {
                            "content_type": "DraftIssue",
                            "content": {"title": "Unassigned idea", "body": "later"},
                            "fields": [
                                {
                                    "id": 407,
                                    "value": {
                                        "id": "backlog",
                                        "name": {"raw": "Backlog"},
                                    },
                                }
                            ],
                        },
                    ]
                )
            ],
        }
    )
    database, provider = project_responses(config(), transport)
    service = ProjectSnapshotService(provider, database, minimum_refresh_seconds=0)
    app = create_app(config(), database=database, service=service)

    with TestClient(app) as client:
        result = client.get("/api/project")

    assert result.status_code == 200
    payload = result.json()
    assert [column["status"] for column in payload["columns"]] == [
        "Backlog",
        "Todo",
        "Done",
        "No status",
    ]
    assert payload["fetched_at"]
    assert payload["rate_limited_until"] is None
    backlog = payload["columns"][0]["items"][0]
    assert backlog == {
        "type": "DraftIssue",
        "repository": None,
        "number": None,
        "title": "Unassigned idea",
        "url": None,
        "state": None,
        "labels": [],
        "linked_issue_numbers": [],
    }
    issue = payload["columns"][1]["items"][0]
    assert issue == {
        "type": "Issue",
        "repository": "TychoHenzen/BeeHAIve",
        "number": 7,
        "title": "Build board",
        "url": "https://github.com/TychoHenzen/BeeHAIve/issues/7",
        "state": "open",
        "labels": ["enhancement"],
        "linked_issue_numbers": [],
    }
    pull_request = payload["columns"][2]["items"][0]
    assert pull_request == {
        "type": "PullRequest",
        "repository": "TychoHenzen/BeeHAIve",
        "number": 8,
        "title": "Ship board",
        "url": "https://github.com/TychoHenzen/BeeHAIve/pull/8",
        "state": "open",
        "labels": [],
        "linked_issue_numbers": [7, 12],
    }
    database.close()


def test_cursor_pagination_and_read_only_api() -> None:
    project_url = "https://api.github.com/users/TychoHenzen/projectsV2/2"
    fields_url = f"{project_url}/fields"
    first_items_url = f"{project_url}/items?per_page=100&fields=407"
    next_items_url = f"{project_url}/items?page=2"
    transport = FakeTransport(
        {
            project_url: [response({"id": 1})],
            fields_url: [
                response(
                    [
                        {
                            "id": 407,
                            "name": "Status",
                            "data_type": "single_select",
                            "options": [{"id": "todo", "name": "Todo"}],
                        }
                    ]
                )
            ],
            first_items_url: [
                response(
                    [],
                    headers={"Link": f'<{next_items_url}>; rel="next"'},
                )
            ],
            next_items_url: [response([])],
        }
    )
    database, provider = project_responses(config(), transport)
    app = create_app(
        config(),
        database=database,
        service=ProjectSnapshotService(provider, database, 60),
    )

    with TestClient(app) as client:
        result = client.get("/api/project")
        refresh = client.post("/api/project/refresh")
        page = client.get("/")
        script = client.get("/web/app.js")

    assert result.status_code == 200
    assert refresh.status_code == 200
    assert refresh.json() == result.json()
    assert [call[0] for call in transport.calls] == [
        project_url,
        fields_url,
        first_items_url,
        next_items_url,
    ]
    assert page.status_code == 200
    assert all(label in page.text for label in ("Board", "Workflows", "Agents", "Hive"))
    assert script.status_code == 200
    assert "fetch" in script.text
    assert "held by agent X" not in script.text
    assert "unclaimed" in script.text
    assert 'load("/api/project/refresh", "POST")' in script.text
    assert set(app.openapi()["paths"]["/api/project"]) == {"get"}
    assert set(app.openapi()["paths"]["/api/project/refresh"]) == {"post"}
    database.close()


def test_transport_io_failure_returns_controlled_api_error() -> None:
    database = SnapshotDatabase(":memory:")

    def failing_transport(_url: str, _headers: Mapping[str, str]) -> HttpResponse:
        raise URLError("offline")

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
