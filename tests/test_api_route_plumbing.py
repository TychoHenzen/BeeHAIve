from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi import HTTPException

from beehaiive.agent_stations import AgentStationServiceError
from beehaiive.api.helpers.assets import docs_asset
from beehaiive.api.routes.agent_stations import _http_error as station_http_error


def test_docs_asset_resolves_repo_docs_and_media_type() -> None:
    response = docs_asset("dashboard.js", media_type="application/javascript")

    assert Path(response.path) == Path(__file__).parents[1] / "docs" / "dashboard.js"
    assert response.media_type == "application/javascript"


@pytest.mark.parametrize(
    ("mapper", "error", "status"),
    [
        (
            station_http_error,
            AgentStationServiceError("invalid_action", "unsupported action"),
            422,
        ),
    ],
)
def test_service_error_policies_preserve_status_boundaries(
    mapper: Callable[[object], HTTPException], error: object, status: int
) -> None:
    response = mapper(error)

    assert response.status_code == status


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (AgentStationServiceError("not_found", "missing"), 404),
        (AgentStationServiceError("persistence", "database unavailable"), 409),
        (AgentStationServiceError("unexpected", "unexpected failure"), 400),
    ],
)
def test_station_error_policy_keeps_intentional_statuses(
    error: AgentStationServiceError, status: int
) -> None:
    response = station_http_error(error)

    assert response.status_code == status
    assert response.detail == str(error)
