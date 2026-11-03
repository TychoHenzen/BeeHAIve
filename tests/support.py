from __future__ import annotations

import copy
import json
import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from beehaiive.config import CoreConfig
from beehaiive.database import SnapshotDatabase
from beehaiive.models import ProjectCard, ProjectColumn, ProjectSnapshot
from beehaiive.project import ProjectDataError, ProjectProvider
from beehaiive.rest import GithubRestClient, HttpResponse, RateLimitError


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


FETCHED_AT = "2026-09-29T10:00:00+00:00"
PROJECT_URL = "https://api.github.com/users/TychoHenzen/projectsV2/2"
FIELDS_URL = f"{PROJECT_URL}/fields"
ITEMS_URL = f"{PROJECT_URL}/items?per_page=100&fields=407"
REPOSITORY = "TychoHenzen/BeeHAIve"
REPOSITORY_URL = f"https://github.com/{REPOSITORY}"
OPTION_IDS = (("todo", "Todo"), ("done", "Done"))


def option(option_id: str, name: str, raw: bool = False) -> dict[str, Any]:
    return {"id": option_id, "name": {"raw": name} if raw else name}


def status_field(*options: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": 407,
        "name": "Status",
        "data_type": "single_select",
        "options": list(options),
    }


def status_value(
    option_id: str, name: str | None = None, field_id: int | str = 407
) -> dict[str, Any]:
    value: dict[str, Any] = {"id": option_id}
    if name is not None:
        value["name"] = {"raw": name}
    return {"id": field_id, "value": value}


def project_item(
    content_type: str, content: dict[str, Any], *values: dict[str, Any]
) -> dict[str, Any]:
    return {"content_type": content_type, "content": content, "fields": list(values)}


def issue_content(number: int, title: str, **extra: Any) -> dict[str, Any]:
    base = {
        "number": number,
        "title": title,
        "html_url": f"{REPOSITORY_URL}/issues/{number}",
        "repository": {"full_name": REPOSITORY},
    }
    return base | extra


EMPTY_CARD: dict[str, Any] = {
    "type": None,
    "repository": None,
    "number": None,
    "title": None,
    "url": None,
    "state": None,
    "labels": [],
    "linked_issue_numbers": [],
}


def expected_card(**fields: Any) -> dict[str, Any]:
    return copy.deepcopy(EMPTY_CARD) | fields


def http_error(
    status_code: int, headers: Mapping[str, str], message: str
) -> HttpResponse:
    body = json.dumps({"message": message})
    return HttpResponse(status_code=status_code, headers=headers, body=body)


def stored_snapshot(*statuses: str) -> ProjectSnapshot:
    columns = tuple(ProjectColumn(status=status, items=()) for status in statuses)
    return ProjectSnapshot(
        fetched_at=FETCHED_AT,
        rate_limited_until=None,
        columns=columns,
        status_field_id="407",
        status_option_ids=OPTION_IDS,
    )


def bare_snapshot() -> ProjectSnapshot:
    columns = (ProjectColumn(status="Todo", items=()),)
    return ProjectSnapshot(
        fetched_at=FETCHED_AT, rate_limited_until=None, columns=columns
    )


def card(item_key: str, title: str) -> ProjectCard:
    return ProjectCard(
        type="Issue",
        repository="acme/repo",
        number=1,
        title=title,
        url=None,
        state="open",
        labels=(),
        linked_issue_numbers=(),
        item_key=item_key,
    )


def held_snapshot() -> ProjectSnapshot:
    columns = (ProjectColumn("Todo", (card("held", "held"),)),)
    return ProjectSnapshot(
        fetched_at=FETCHED_AT, rate_limited_until=None, columns=columns
    )


def raising(error: Exception) -> Callable[..., Any]:
    def fail(*_args: Any, **_kwargs: Any) -> Any:
        raise error

    return fail


def returning(value: Any) -> Callable[..., Any]:
    return lambda *_args, **_kwargs: value


def workflow_state(state_id: str, title: str, action: str) -> dict[str, Any]:
    return {"id": state_id, "title": title, "action": action, "max_visits": 1}


def workflow_transition(
    source: str, target: str, condition: dict[str, Any]
) -> dict[str, Any]:
    return {"from": source, "to": target, "priority": 1, "conditions": [condition]}


VALID_DEFINITION: dict[str, Any] = {
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
        workflow_state("wait", "Wait", "wait_for_work"),
        workflow_state("escalate", "Escalate", "escalate"),
    ],
    "transitions": [
        workflow_transition(
            "wait", "escalate", {"kind": "item_status_is", "value": "{status}"}
        ),
        workflow_transition("escalate", "wait", {"kind": "always"}),
    ],
}


def valid_definition() -> dict[str, Any]:
    return copy.deepcopy(VALID_DEFINITION)


class BlockingProvider:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def fetch_snapshot(self) -> ProjectSnapshot:
        self.calls += 1
        self.started.set()
        assert self.release.wait(timeout=2)
        return bare_snapshot()


class FailingProvider:
    def fetch_snapshot(self) -> ProjectSnapshot:
        raise ProjectDataError("project unavailable")


class BlankProvider:
    def fetch_snapshot(self) -> ProjectSnapshot:
        return ProjectSnapshot(None, None, ())


class LimitedProvider:
    def __init__(self) -> None:
        self.fetch_item_args: tuple[object, ...] | None = None

    def fetch_snapshot(self) -> ProjectSnapshot:
        until = datetime(2026, 9, 29, 11, tzinfo=UTC)
        raise RateLimitError("limited", until, primary=False)

    def fetch_item(self, item_key: str, **options: Any) -> tuple[ProjectCard, str]:
        self.fetch_item_args = (
            item_key,
            options.get("status_field_id"),
            options.get("status_options"),
            options.get("status_option_ids"),
        )
        return card(item_key, "Issue"), "Todo"


class SnapshotReader:
    def __init__(self, snapshot: ProjectSnapshot) -> None:
        self.snapshot = snapshot

    def get_snapshot(self) -> ProjectSnapshot:
        return self.snapshot


class HolderReader:
    def holders(self) -> dict[str, dict[str, str]]:
        return {"held": {"id": "agent"}}


class UrlopenResponse:
    status = 200
    headers = {"ETag": "one"}

    def __enter__(self) -> UrlopenResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return b"{}"
