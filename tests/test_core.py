from __future__ import annotations

import io
import json
import runpy
import sqlite3
import threading
import urllib.error
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.error import URLError

import pytest
from fastapi.testclient import TestClient

import beehaiive.__main__ as main_module
import beehaiive.app as app_module
import beehaiive.config as config_module
import beehaiive.project as project_module
import beehaiive.rest as rest_module
from beehaiive.agents import AgentConflict, AgentError, AgentService
from beehaiive.app import create_app
from beehaiive.config import CoreConfig, CoreConfigurationError
from beehaiive.database import SnapshotDatabase
from beehaiive.models import ProjectCard, ProjectColumn, ProjectSnapshot
from beehaiive.project import (
    ProjectDataError,
    ProjectProvider,
    parse_closing_issue_numbers,
)
from beehaiive.rest import (
    GithubResponseError,
    GithubRestClient,
    HttpResponse,
    RateLimitError,
)
from beehaiive.snapshot import ProjectSnapshotService
from beehaiive.workflows import WorkflowStore


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


def test_project_provider_fetches_one_held_item_with_cached_status_field() -> None:
    project_url = "https://api.github.com/users/TychoHenzen/projectsV2/2"
    item_url = f"{project_url}/items/project-item-7?fields=407"
    transport = FakeTransport(
        {
            item_url: [
                response(
                    {
                        "id": "project-item-7",
                        "content_type": "Issue",
                        "content": {
                            "number": 7,
                            "title": "Fresh item",
                            "html_url": "https://github.com/TychoHenzen/BeeHAIve/issues/7",
                            "repository": {"full_name": "TychoHenzen/BeeHAIve"},
                        },
                        "fields": [{"id": 407, "value": {"id": "done"}}],
                    }
                )
            ]
        }
    )
    database, provider = project_responses(config(), transport)
    database.save_snapshot(
        ProjectSnapshot(
            fetched_at="2026-09-29T10:00:00+00:00",
            rate_limited_until=None,
            columns=(
                ProjectColumn(status="Todo", items=()),
                ProjectColumn(status="Done", items=()),
            ),
            status_field_id="407",
            status_option_ids=(("todo", "Todo"), ("done", "Done")),
        )
    )
    service = ProjectSnapshotService(provider, database, minimum_refresh_seconds=0)

    card, status = service.fetch_held_item("project-item-7")

    assert card.item_key == "project-item-7"
    assert status == "Done"
    assert [call[0] for call in transport.calls] == [item_url]
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
    assert "held by agent X" not in script.text
    assert "unclaimed" in script.text
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
            status_field_id="407",
            status_option_ids=(("todo", "Todo"), ("done", "Done")),
        )
    )
    first.close()
    second = SnapshotDatabase(path)
    loaded = second.load_snapshot()
    assert loaded is not None
    assert loaded.status_field_id == "407"
    assert loaded.status_option_ids == (("todo", "Todo"), ("done", "Done"))
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version FROM schema_version").fetchone() == (
            5,
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


def test_config_environment_and_validation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "\n".join(
            (
                "export BEEHAIIVE_PROJECT_OWNER=dotenv-owner",
                "BEEHAIIVE_PROJECT_OWNER_TYPE='org'",
                "BEEHAIIVE_PROJECT_NUMBER=7",
                "GITHUB_TOKEN='dotenv-token'",
                "BEEHAIIVE_DB='custom.db'",
                "BEEHAIIVE_CODEX='codex-custom'",
                "BEEHAIIVE_CODEX_ARGS=--model 'fast model'",
                "BEEHAIIVE_SKILLS_DIRS=one;two",
                "BEEHAIIVE_PROJECT_REFRESH_SECONDS=120",
                "BEEHAIIVE_AGENT_STEP_TIMEOUT_SECONDS=12",
                "ignored line",
            )
        ),
        encoding="utf-8",
    )
    loaded = CoreConfig.from_environment(
        environ={
            "BEEHAIIVE_PROJECT_OWNER": "override-owner",
            "BEEHAIIVE_PROJECT_NUMBER": "9",
        },
        dotenv_path=dotenv,
    )
    assert loaded.owner == "override-owner"
    assert loaded.owner_type == "org"
    assert loaded.project_number == 9
    assert loaded.github_token == "dotenv-token"
    assert loaded.codex_args == ("--model", "fast model")
    assert loaded.skills_dirs == (Path("one"), Path("two"))
    assert loaded.refresh_seconds == 120
    assert loaded.agent_step_timeout_seconds == 12

    monkeypatch.setattr(config_module, "_gh_auth_token", lambda: "gh-token")
    fallback = CoreConfig.from_environment(
        environ={
            "BEEHAIIVE_PROJECT_OWNER": "owner",
            "BEEHAIIVE_PROJECT_NUMBER": "2",
        },
        dotenv_path=tmp_path / "missing.env",
    )
    assert fallback.github_token == "gh-token"

    invalid_values = (
        ({"BEEHAIIVE_PROJECT_OWNER_TYPE": "team"}, "OWNER_TYPE"),
        ({"BEEHAIIVE_PROJECT_NUMBER": "not-a-number"}, "must be an integer"),
        ({"BEEHAIIVE_PROJECT_REFRESH_SECONDS": "nope"}, "REFRESH"),
        ({"BEEHAIIVE_AGENT_STEP_TIMEOUT_SECONDS": "nope"}, "AGENT_STEP_TIMEOUT"),
        ({"BEEHAIIVE_CODEX_ARGS": "'unterminated"}, "shell-like"),
    )
    for overrides, message in invalid_values:
        values = {
            "BEEHAIIVE_PROJECT_OWNER": "owner",
            "BEEHAIIVE_PROJECT_NUMBER": "2",
            "GITHUB_TOKEN": "token",
            **overrides,
        }
        with pytest.raises(CoreConfigurationError, match=message):
            CoreConfig.from_environment(values, dotenv_path=tmp_path / "missing.env")

    with pytest.raises(CoreConfigurationError, match="OWNER_TYPE"):
        CoreConfig(owner="owner", owner_type="team", project_number=2)
    with pytest.raises(CoreConfigurationError, match="positive"):
        CoreConfig(owner="owner", owner_type="user", project_number=0)
    with pytest.raises(CoreConfigurationError, match="between 1 and 900"):
        CoreConfig(
            owner="owner",
            owner_type="user",
            project_number=2,
            agent_step_timeout_seconds=901,
        )


def test_rest_helpers_and_controlled_failures() -> None:
    assert rest_module._with_query("https://example.test/items?a=1", {"b": 2}) == (
        "https://example.test/items?a=1&b=2"
    )
    assert rest_module._with_query("https://example.test/items", None) == (
        "https://example.test/items"
    )
    assert rest_module._normalise_headers({"ETag": "abc"}) == {"etag": "abc"}
    assert rest_module._body_text(b"body") == "body"
    assert rest_module._decode_json(b'{"ok":true}', "url") == {"ok": True}
    with pytest.raises(GithubResponseError, match="invalid JSON"):
        rest_module._decode_json("not-json", "url")
    assert rest_module._response_message("") == "empty response"
    assert rest_module._response_message('{"message":"bad"}') == "bad"
    assert rest_module._response_message("plain") == "plain"
    assert rest_module._response_message("[1, 2]") == "[1, 2]"
    assert rest_module._is_rate_limit_response(429, {}, "")
    assert rest_module._is_rate_limit_response(403, {"retry-after": "1"}, "")
    assert rest_module._is_rate_limit_response(403, {}, "rate limit exceeded")
    assert not rest_module._is_rate_limit_response(403, {}, "forbidden")
    assert rest_module._parse_int(" 2 ") == 2
    assert rest_module._parse_int("nope") is None
    assert rest_module._parse_float(" 2.5 ") == 2.5
    assert rest_module._parse_float("nope") is None
    assert rest_module.next_link({}) is None
    assert rest_module.next_link({"Link": '<https://example.test/2>; rel="next"'}) == (
        "https://example.test/2"
    )
    assert rest_module.next_link({"link": "<https://example.test/2>; rel=last"}) is None

    database = SnapshotDatabase(":memory:")
    client = GithubRestClient(
        "token",
        database,
        transport=lambda _url, _headers: HttpResponse(304, {}, ""),
    )
    with pytest.raises(GithubResponseError, match="304 without"):
        client.get_json("https://example.test/missing")
    client = GithubRestClient(
        "token",
        database,
        transport=lambda _url, _headers: HttpResponse(
            500, {}, '{"message":"server broke"}'
        ),
    )
    with pytest.raises(GithubResponseError, match="server broke"):
        client.get_json("https://example.test/error")
    client = GithubRestClient(
        "token",
        database,
        transport=lambda _url, _headers: HttpResponse(200, {}, "not-json"),
    )
    with pytest.raises(GithubResponseError, match="invalid JSON"):
        client.get_json("https://example.test/invalid")
    database.close()


def test_project_parsing_rejects_malformed_shapes_and_normalizes_items() -> None:
    with pytest.raises(ProjectDataError, match="did not contain"):
        project_module._list_payload({}, "items")
    with pytest.raises(ProjectDataError, match="exactly one"):
        project_module._status_field([])
    with pytest.raises(ProjectDataError, match="not single-select"):
        project_module._status_field(
            [{"id": "status", "name": "Status", "data_type": "text", "options": []}]
        )
    with pytest.raises(ProjectDataError, match="no options"):
        project_module._status_field(
            [{"id": "status", "name": "Status", "data_type": "single_select"}]
        )
    with pytest.raises(ProjectDataError, match="no id"):
        project_module._status_field(
            [{"name": "Status", "data_type": "single_select", "options": []}]
        )

    field = {
        "id": "status",
        "name": "Status",
        "dataType": "single-select",
        "options": [
            {"id": "todo", "name": {"raw": "Todo"}},
            {"id": "done", "name": "Done"},
        ],
    }
    assert project_module._status_options(field) == ("Todo", "Done")
    assert project_module._status_option_ids(field) == {"todo": "Todo", "done": "Done"}

    pull_request = project_module._normalise_item(
        {
            "id": "item-1",
            "content_type": "PullRequest",
            "content": {
                "repository_url": "https://api.github.com/repos/acme/repo",
                "number": "4",
                "title": "PR",
                "body": "Closes #7",
                "labels": [{"name": "bug"}, "urgent"],
                "html_url": "https://github.com/acme/repo/pull/4",
                "state": "open",
            },
            "fields": [{"id": "status", "value": {"id": "todo"}}],
        }
    )
    assert pull_request is not None
    assert pull_request.repository == "acme/repo"
    assert pull_request.number == 4
    assert pull_request.labels == ("bug", "urgent")
    assert pull_request.linked_issue_numbers == (7,)
    draft = project_module._normalise_item(
        {"content_type": "DraftIssue", "title": "draft", "content": {}}
    )
    assert draft is not None
    assert draft.repository is None
    assert draft.number is None
    assert project_module._normalise_item({"content_type": "Milestone"}) is None
    assert (
        project_module._item_status(
            {"fields": [{"id": "other", "name": "Status", "value": "Todo"}]},
            "status",
            ("Todo",),
            {},
        )
        == "Todo"
    )
    assert (
        project_module._item_status(
            {
                "fields": [
                    {"id": "other", "name": "Other", "value": "ignored"},
                    {"id": "status", "value": "Todo"},
                ]
            },
            "status",
            ("Todo",),
            {},
        )
        == "Todo"
    )
    assert project_module._item_status({}, "status", ("Todo",), {}) == "No status"
    assert (
        project_module._repository_name({"repo": {"name_with_owner": "a/b"}}) == "a/b"
    )
    assert project_module._repository_name({"repository_url": "https://x/a/b"}) == "a/b"
    assert project_module._labels(None) == ()
    assert project_module._optional_int(True) is None
    assert project_module._optional_int("4") == 4


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


def test_entrypoint_calls_uvicorn_on_the_core_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_value = config()
    monkeypatch.setattr(
        main_module.CoreConfig,
        "from_environment",
        classmethod(lambda _cls: config_value),
    )
    monkeypatch.setattr(main_module, "create_app", lambda value: value)
    calls: list[tuple[object, dict[str, object]]] = []
    monkeypatch.setattr(
        main_module.uvicorn,
        "run",
        lambda application, **options: calls.append((application, options)),
    )
    main_module.main()
    assert calls == [
        (
            config_value,
            {"host": "127.0.0.1", "port": 8000},
        )
    ]
    monkeypatch.setattr(app_module, "create_app", lambda value: value)
    runpy.run_module("beehaiive.__main__", run_name="__main__")
    assert len(calls) == 2


def test_config_gh_token_success_and_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    result = type("Result", (), {"stdout": " gh-token \n"})()
    monkeypatch.setattr(
        config_module.subprocess,
        "run",
        lambda *_args, **_kwargs: result,
    )
    assert config_module._gh_auth_token() == "gh-token"

    def fail(*_args: object, **_kwargs: object) -> object:
        raise config_module.subprocess.CalledProcessError(1, "gh")

    monkeypatch.setattr(config_module.subprocess, "run", fail)
    with pytest.raises(CoreConfigurationError, match="gh auth token"):
        config_module._gh_auth_token()


def test_database_rejects_unknown_and_corrupt_persisted_state(tmp_path: Path) -> None:
    unsupported = tmp_path / "unsupported.db"
    connection = sqlite3.connect(unsupported)
    connection.execute("CREATE TABLE schema_version(version INTEGER NOT NULL)")
    connection.execute("INSERT INTO schema_version VALUES (999)")
    connection.commit()
    connection.close()
    with pytest.raises(RuntimeError, match="Unsupported schema"):
        SnapshotDatabase(unsupported)

    database = SnapshotDatabase(":memory:")
    database.transaction(
        lambda connection: connection.execute(
            "INSERT INTO http_cache(url, etag, body, headers) VALUES (?, ?, ?, ?)",
            ("bad", None, "{}", "not-json"),
        )
    )
    cached = database.get_http_cache("bad")
    assert cached is not None
    assert cached.headers == {}
    database.transaction(
        lambda connection: connection.execute(
            "INSERT INTO project_snapshot(id, payload) VALUES (1, ?)",
            ("not-json",),
        )
    )
    with pytest.raises(RuntimeError, match="Persisted project"):
        database.load_snapshot()
    database.close()


def test_model_decoding_rejects_boolean_numbers_and_filters_values() -> None:
    snapshot = ProjectSnapshot.from_dict(
        {
            "columns": [
                {
                    "status": "Todo",
                    "items": [
                        {
                            "number": True,
                            "labels": ["good", True, {"bad": "label"}],
                            "linked_issue_numbers": ["7", True, "bad"],
                        }
                    ],
                }
            ]
        }
    )
    item = snapshot.columns[0].items[0]
    assert item.number is None
    assert item.labels == ("good", "True")
    assert item.linked_issue_numbers == (7,)


def test_project_fetch_item_supports_nested_items_and_rejects_unknown_content() -> None:
    project_url = "https://api.github.com/users/TychoHenzen/projectsV2/2"
    field = {
        "id": 407,
        "name": "Status",
        "data_type": "single_select",
        "options": [{"id": "todo", "name": "Todo"}],
    }
    item_url = f"{project_url}/items/item%2F1?fields=407"
    transport = FakeTransport(
        {
            f"{project_url}/fields": [response({"fields": [field]})],
            item_url: [
                response(
                    {
                        "item": {
                            "content_type": "Issue",
                            "content": {
                                "repository": "acme/repo",
                                "number": 1,
                                "title": "Issue",
                            },
                            "fields": [{"id": "407", "value": {"id": "todo"}}],
                        }
                    }
                )
            ],
        }
    )
    database, provider = project_responses(config(), transport)
    card, status = provider.fetch_item("item/1", status_field_id=None)
    assert card.repository == "acme/repo"
    assert status == "Todo"
    database.close()

    bad_transport = FakeTransport(
        {
            f"{project_url}/items/bad?fields=407": [
                response({"content_type": "Unsupported"})
            ]
        }
    )
    database, provider = project_responses(config(), bad_transport)
    with pytest.raises(ProjectDataError, match="unavailable"):
        provider.fetch_item("bad", status_field_id="407", status_options=("Todo",))
    database.close()
    assert project_module._repository_name({"repository": "acme/repo"}) == "acme/repo"
    assert project_module._display_name({"raw": ""}) == ""


def test_project_snapshot_skips_unknown_items_and_handles_rate_limits() -> None:
    project_url = "https://api.github.com/users/TychoHenzen/projectsV2/2"
    fields_url = f"{project_url}/fields"
    items_url = f"{project_url}/items?per_page=100&fields=407"
    field = {
        "id": 407,
        "name": "Status",
        "data_type": "single_select",
        "options": [{"id": "todo", "name": "Todo"}],
    }
    transport = FakeTransport(
        {
            project_url: [response({"id": 2})],
            fields_url: [response({"fields": [field]})],
            items_url: [response({"items": [{"content_type": "Unsupported"}]})],
        }
    )
    database, provider = project_responses(config(), transport)
    snapshot = provider.fetch_snapshot()
    assert snapshot.columns[-1].items == ()
    database.close()

    class LimitedProvider:
        def __init__(self) -> None:
            self.fetch_item_args: tuple[object, ...] | None = None

        def fetch_snapshot(self) -> ProjectSnapshot:
            raise RateLimitError(
                "limited",
                datetime(2026, 9, 29, 11, tzinfo=UTC),
                primary=False,
            )

        def fetch_item(
            self,
            item_key: str,
            *,
            status_field_id: str | None,
            status_options: tuple[str, ...],
            status_option_ids: dict[str, str] | None = None,
        ) -> tuple[ProjectCard, str]:
            self.fetch_item_args = (
                item_key,
                status_field_id,
                status_options,
                status_option_ids,
            )
            return ProjectCard(
                type="Issue",
                repository="acme/repo",
                number=1,
                title="Issue",
                url=None,
                state="open",
                labels=(),
                linked_issue_numbers=(),
                item_key=item_key,
            ), "Todo"

    limited_provider = LimitedProvider()
    limited_database = SnapshotDatabase(":memory:")
    service = ProjectSnapshotService(
        limited_provider,
        limited_database,
        minimum_refresh_seconds=60,
        clock=lambda: datetime(2026, 9, 29, 10, tzinfo=UTC),
    )
    held, status = service.fetch_held_item("item-1")
    assert held.item_key == "item-1"
    assert status == "Todo"
    assert limited_provider.fetch_item_args == ("item-1", None, (), {})
    assert service._refresh_due(
        ProjectSnapshot(None, None, (ProjectColumn("Todo", ()),))
    )
    assert not service._refresh_due(
        ProjectSnapshot(
            "2026-09-29T09:59:30+00:00",
            "2026-09-29T11:00:00+00:00",
            (ProjectColumn("Todo", ()),),
        )
    )
    assert service._refresh_due(
        ProjectSnapshot("invalid", None, (ProjectColumn("Todo", ()),))
    )
    assert service._refresh_due(
        ProjectSnapshot(
            "2026-09-29T09:59:30+00:00",
            "invalid",
            (ProjectColumn("Todo", ()),),
        )
    )
    limited_database.close()


def test_rest_urlopen_handles_success_and_http_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Response:
        status = 200
        headers = {"ETag": "one"}

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return b"{}"

    monkeypatch.setattr(
        rest_module.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: Response(),
    )
    success = rest_module._urlopen("https://example.test", {})
    assert success.status_code == 200
    assert success.body == b"{}"

    error = urllib.error.HTTPError(
        "https://example.test",
        403,
        "forbidden",
        {"X-Error": "yes"},
        io.BytesIO(b"forbidden"),
    )
    monkeypatch.setattr(
        rest_module.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(error),
    )
    failed = rest_module._urlopen("https://example.test", {})
    assert failed.status_code == 403
    assert failed.body == b"forbidden"
