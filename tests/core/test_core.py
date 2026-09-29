from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.error import URLError

import pytest
from fastapi.testclient import TestClient

from beehaiive.core.app import create_app
from beehaiive.core.config import CoreConfig, CoreConfigurationError
from beehaiive.core.database import SnapshotDatabase
from beehaiive.core.models import ProjectColumn, ProjectSnapshot
from beehaiive.core.project import (
    ProjectDataError,
    ProjectProvider,
    parse_closing_issue_numbers,
)
from beehaiive.core.rest import (
    GithubResponseError,
    GithubRestClient,
    HttpResponse,
    RateLimitError,
)
from beehaiive.core.snapshot import ProjectSnapshotService


class FakeTransport:
    def __init__(self, responses: Mapping[str, list[HttpResponse]]) -> None:
        self.responses = {url: list(values) for url, values in responses.items()}
        self.calls: list[tuple[str, Mapping[str, str]]] = []

    def __call__(self, url: str, headers: Mapping[str, str]) -> HttpResponse:
        self.calls.append((url, headers))
        values = self.responses.get(url)
        if not values:
            raise AssertionError(f"No fake response for {url}")
        return values.pop(0)


def response(value: Any, *, headers: Mapping[str, str] | None = None) -> HttpResponse:
    return HttpResponse(
        status_code=200,
        headers=headers or {},
        body=json.dumps(value),
    )


def config(owner_type: str = "user", refresh_seconds: float = 0) -> CoreConfig:
    return CoreConfig(
        owner="TychoHenzen",
        owner_type=owner_type,
        project_number=2,
        github_token="test-token",
        refresh_seconds=refresh_seconds,
    )


def project_responses(
    config_value: CoreConfig, transport: FakeTransport
) -> tuple[SnapshotDatabase, ProjectProvider]:
    database = SnapshotDatabase(":memory:")
    client = GithubRestClient(
        config_value.github_token,
        database,
        transport=transport,
    )
    return database, ProjectProvider(config_value, client)


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


def test_project_provider_uses_org_prefix_and_rejects_ambiguous_status() -> None:
    org_config = config("org")
    transport = FakeTransport({})
    database, provider = project_responses(org_config, transport)
    assert (
        provider.project_url == "https://api.github.com/orgs/TychoHenzen/projectsV2/2"
    )
    database.close()

    with pytest.raises(CoreConfigurationError):
        CoreConfig.from_environment(
            {
                "BEEHAIIVE_PROJECT_OWNER": "owner",
                "BEEHAIIVE_PROJECT_OWNER_TYPE": "team",
                "BEEHAIIVE_PROJECT_NUMBER": "2",
                "GITHUB_TOKEN": "token",
            },
            dotenv_path=Path("missing.env"),
        )

    project_url = "https://api.github.com/users/TychoHenzen/projectsV2/2"
    for status_fields in ([], [{"id": 1, "name": "Status", "options": []}] * 2):
        invalid_transport = FakeTransport(
            {
                project_url: [response({"id": 1})],
                f"{project_url}/fields": [response(status_fields)],
            }
        )
        invalid_database, invalid_provider = project_responses(
            config(), invalid_transport
        )
        with pytest.raises(ProjectDataError):
            invalid_provider.fetch_snapshot()
        invalid_database.close()


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
    assert "held by agent X" in script.text
    assert 'load("/api/project/refresh", "POST")' in script.text
    assert set(app.openapi()["paths"]["/api/project"]) == {"get"}
    assert set(app.openapi()["paths"]["/api/project/refresh"]) == {"post"}
    database.close()


def test_etag_reuses_cached_body_and_rate_limit_uses_headers() -> None:
    url = "https://api.github.com/users/owner/projectsV2/2"
    database = SnapshotDatabase(":memory:")
    transport = FakeTransport(
        {
            url: [
                response([], headers={"ETag": '"abc"'}),
                HttpResponse(status_code=304, headers={}, body=""),
            ]
        }
    )
    client = GithubRestClient("token", database, transport=transport)
    assert client.get_json(url).value == []
    cached = client.get_json(url)
    assert cached.from_cache
    assert transport.calls[1][1]["If-None-Match"] == '"abc"'

    reset = int((datetime.now(UTC) + timedelta(minutes=2)).timestamp())
    limited = FakeTransport(
        {
            url + "/fields": [
                HttpResponse(
                    status_code=403,
                    headers={
                        "X-RateLimit-Remaining": "0",
                        "X-RateLimit-Reset": str(reset),
                    },
                    body='{"message":"slow down"}',
                )
            ]
        }
    )
    limited_database = SnapshotDatabase(":memory:")
    limited_client = GithubRestClient("token", limited_database, transport=limited)
    with pytest.raises(RateLimitError) as raised:
        limited_client.get_json(url + "/fields")
    assert raised.value.primary
    assert raised.value.rate_limited_until.timestamp() >= reset
    with pytest.raises(RateLimitError) as blocked:
        limited_client.get_json(url + "/fields")
    assert blocked.value.primary
    assert len(limited.calls) == 1
    limited_database.close()
    database.close()


def test_secondary_deadlines_and_429_are_enforced() -> None:
    url = "https://api.github.com/users/owner/projectsV2/2"
    now = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
    current = [now]

    def secondary_response(
        status_code: int, headers: Mapping[str, str]
    ) -> HttpResponse:
        return HttpResponse(
            status_code=status_code,
            headers=headers,
            body='{"message":"You have exceeded a secondary rate limit"}',
        )

    transport = FakeTransport(
        {
            url: [
                secondary_response(
                    403,
                    {
                        "X-RateLimit-Remaining": "10",
                        "Retry-After": "7",
                    },
                ),
                secondary_response(403, {"X-RateLimit-Remaining": "10"}),
                secondary_response(403, {"X-RateLimit-Remaining": "10"}),
                secondary_response(
                    429,
                    {
                        "X-RateLimit-Remaining": "10",
                        "Retry-After": "5",
                    },
                ),
            ]
        }
    )
    database = SnapshotDatabase(":memory:")
    client = GithubRestClient(
        "token", database, transport=transport, clock=lambda: current[0]
    )

    with pytest.raises(RateLimitError) as retry_after:
        client.get_json(url)
    assert not retry_after.value.primary
    assert retry_after.value.rate_limited_until == now + timedelta(seconds=7)

    with pytest.raises(RateLimitError) as blocked:
        client.get_json(url)
    assert blocked.value.rate_limited_until == retry_after.value.rate_limited_until
    assert len(transport.calls) == 1

    current[0] += timedelta(seconds=7)
    with pytest.raises(RateLimitError) as first_backoff:
        client.get_json(url)
    assert first_backoff.value.rate_limited_until == current[0] + timedelta(seconds=60)

    current[0] += timedelta(seconds=60)
    with pytest.raises(RateLimitError) as second_backoff:
        client.get_json(url)
    assert second_backoff.value.rate_limited_until == current[0] + timedelta(
        seconds=120
    )

    current[0] += timedelta(seconds=120)
    with pytest.raises(RateLimitError) as too_many_requests:
        client.get_json(url)
    assert not too_many_requests.value.primary
    assert too_many_requests.value.rate_limited_until == current[0] + timedelta(
        seconds=5
    )
    assert len(transport.calls) == 4
    database.close()


def test_forbidden_response_is_not_treated_as_rate_limit() -> None:
    url = "https://api.github.com/users/owner/projectsV2/2"
    database = SnapshotDatabase(":memory:")
    transport = FakeTransport(
        {
            url: [
                HttpResponse(
                    status_code=403,
                    headers={"X-RateLimit-Remaining": "4999"},
                    body='{"message":"Resource not accessible by integration"}',
                )
            ]
        }
    )
    client = GithubRestClient("token", database, transport=transport)

    with pytest.raises(GithubResponseError, match="403"):
        client.get_json(url)

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


def test_database_persists_schema_and_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "hive.db"
    first = SnapshotDatabase(path)
    first.save_snapshot(
        ProjectSnapshot(
            fetched_at="2026-09-29T10:00:00+00:00",
            rate_limited_until=None,
            columns=(ProjectColumn(status="Todo", items=()),),
        )
    )
    first.close()
    second = SnapshotDatabase(path)
    assert second.load_snapshot() is not None
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version FROM schema_version").fetchone() == (
            2,
        )
    second.close()


def test_refresh_joins_an_in_flight_request() -> None:
    class BlockingProvider:
        def __init__(self) -> None:
            self.started = threading.Event()
            self.release = threading.Event()
            self.calls = 0

        def fetch_snapshot(self) -> ProjectSnapshot:
            self.calls += 1
            self.started.set()
            assert self.release.wait(timeout=2)
            return ProjectSnapshot(
                fetched_at="2026-09-29T10:00:00+00:00",
                rate_limited_until=None,
                columns=(ProjectColumn(status="Todo", items=()),),
            )

    database = SnapshotDatabase(":memory:")
    provider = BlockingProvider()
    service = ProjectSnapshotService(provider, database, minimum_refresh_seconds=0)
    results: list[ProjectSnapshot] = []

    def read() -> None:
        results.append(service.get_snapshot())

    first = threading.Thread(target=read)
    second = threading.Thread(target=read)
    first.start()
    assert provider.started.wait(timeout=2)
    second.start()
    provider.release.set()
    first.join(timeout=2)
    second.join(timeout=2)
    assert provider.calls == 1
    assert len(results) == 2
    database.close()


def test_closing_keywords_are_deduplicated() -> None:
    assert parse_closing_issue_numbers("Closes #7, fixes acme/repo#8, fixes #7") == (
        7,
        8,
    )
