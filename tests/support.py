from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from beehaiive.config import CoreConfig
from beehaiive.database import SnapshotDatabase
from beehaiive.project import ProjectProvider
from beehaiive.rest import GithubRestClient, HttpResponse


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
