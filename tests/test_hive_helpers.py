from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from beehaiive.database import SnapshotDatabase
from beehaiive.hive import (
    HiveProjection,
    _color,
    _condition_matches,
    _duration,
    _json_object,
    _matches_wait_transition,
    _parameter_value,
    _station_map,
    _timestamp,
    _transition_label,
)
from beehaiive.models import ProjectCard

CONDITION_CASES = (
    ({"kind": "always"}, True),
    ({"kind": "item_status_is", "value": "{status}"}, True),
    ({"kind": "item_type_is", "value": "pull_request"}, True),
    ({"kind": "item_has_label", "value": "{label}"}, True),
    ({"kind": "item_lacks_label", "value": "other"}, True),
    ({"kind": "item_repository_is", "value": "owner/repo"}, True),
    ({"kind": "unknown"}, False),
)
STATUS_IS_DONE = {"kind": "item_status_is", "value": "Done"}
STATUS_FROM_PARAMETER = {"kind": "item_status_is", "value": "{status}"}


@pytest.fixture
def card() -> ProjectCard:
    return ProjectCard(
        type="PullRequest",
        repository="owner/repo",
        number=4,
        title="Pull",
        url=None,
        state="open",
        labels=("bug",),
        linked_issue_numbers=(),
        item_key="pull-4",
    )


@pytest.fixture
def parameters() -> dict[str, str]:
    return {"status": "Todo", "label": "bug"}


@pytest.fixture
def definition() -> dict[str, Any]:
    return {
        "initial": "wait",
        "transitions": [
            {"from": "other", "priority": 1, "conditions": []},
            {"from": "wait", "priority": 2, "conditions": [STATUS_IS_DONE]},
            {"from": "wait", "priority": 3, "conditions": [STATUS_FROM_PARAMETER]},
            "bad",
        ],
    }


def test_hive_value_helpers_tolerate_missing_and_malformed_input() -> None:
    assert _json_object("not-json") == {}
    assert _json_object("[]") == {}
    assert _timestamp(None) is None
    assert _timestamp("not-a-date") is None
    assert _duration(None, "2026-09-29T10:00:00+00:00") is None
    assert _duration("2026-09-29T10:00:00+00:00", "2026-09-29T09:00:00+00:00") == 0


def test_hive_parameter_value_substitutes_placeholders(
    parameters: dict[str, str],
) -> None:
    assert _parameter_value(1, parameters) == 1
    assert _parameter_value("{status}", parameters) == "Todo"
    assert _parameter_value("status={status}/{label}", parameters) == "status=Todo/bug"


def test_hive_condition_matches_every_kind(
    card: ProjectCard, parameters: dict[str, str]
) -> None:
    for condition, expected in CONDITION_CASES:
        assert _condition_matches(condition, card, "Todo", parameters) is expected


def test_hive_wait_transition_matches_the_first_open_condition(
    card: ProjectCard, parameters: dict[str, str], definition: dict[str, Any]
) -> None:
    assert _matches_wait_transition(definition, card, "Todo", parameters)
    assert not _matches_wait_transition(
        {"initial": "wait", "transitions": [{"from": "wait", "conditions": {}}]},
        card,
        "Todo",
        parameters,
    )


def test_hive_station_map_transition_label_and_color_helpers() -> None:
    assert _station_map({"states": [{"id": "wait"}, "bad", {"title": "missing"}]}) == {
        "wait": {"id": "wait"}
    }
    assert (
        _transition_label({"conditions": [{"kind": "outcome_is", "value": "done"}]})
        == "done"
    )
    assert _transition_label({"conditions": ["bad"]}) == "always"
    assert _color("agent")


def test_hive_empty_state_and_idempotent_ack() -> None:
    database = SnapshotDatabase(":memory:")
    projection = HiveProjection(
        database, clock=lambda: datetime(2026, 9, 29, tzinfo=UTC)
    )
    assert projection.snapshot() == {
        "generated_at": "2026-09-29T00:00:00+00:00",
        "regions": [],
        "units": [],
        "alerts": [],
        "waiting": {},
    }
    assert projection.events("cursor")["events"] == []
    assert projection.events("cursor")["cursor"] == "cursor"
    assert projection.acknowledge("missing") is None
    database.close()
