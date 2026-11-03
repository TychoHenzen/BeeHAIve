from __future__ import annotations

import threading
from datetime import UTC, datetime
from typing import Any

import pytest

from beehaiive.database import SnapshotDatabase
from beehaiive.models import ProjectColumn, ProjectSnapshot
from beehaiive.project import (
    ProjectDataError,
    parse_closing_issue_numbers,
)
from beehaiive.snapshot import ProjectSnapshotService
from tests.support import (
    FIELDS_URL,
    ITEMS_URL,
    PROJECT_URL,
    BlankProvider,
    BlockingProvider,
    FakeTransport,
    LimitedProvider,
    config,
    issue_content,
    option,
    project_responses,
    response,
    status_field,
    status_value,
    stored_snapshot,
)

REFRESH_COLUMNS = (ProjectColumn("Todo", ()),)


def test_project_provider_fetches_one_held_item_with_cached_status_field() -> None:
    item_url = f"{PROJECT_URL}/items/project-item-7?fields=407"
    item = {
        "id": "project-item-7",
        "content_type": "Issue",
        "content": issue_content(7, "Fresh item"),
        "fields": [status_value("done")],
    }
    transport = FakeTransport({item_url: [response(item)]})
    database, provider = project_responses(config(), transport)
    database.save_snapshot(stored_snapshot("Todo", "Done"))
    service = ProjectSnapshotService(provider, database, minimum_refresh_seconds=0)

    card, status = service.fetch_held_item("project-item-7")

    assert card.item_key == "project-item-7"
    assert status == "Done"
    assert [call[0] for call in transport.calls] == [item_url]
    database.close()


def test_project_provider_uses_org_prefix() -> None:
    org_config = config("org")
    database, provider = project_responses(org_config, FakeTransport({}))
    assert (
        provider.project_url == "https://api.github.com/orgs/TychoHenzen/projectsV2/2"
    )
    database.close()


@pytest.mark.parametrize(
    "status_fields",
    [[], [{"id": 1, "name": "Status", "options": []}] * 2],
    ids=["missing", "ambiguous"],
)
def test_project_snapshot_rejects_missing_or_ambiguous_status_field(
    status_fields: list[dict[str, Any]],
) -> None:
    transport = FakeTransport(
        {
            PROJECT_URL: [response({"id": 1})],
            FIELDS_URL: [response(status_fields)],
        }
    )
    database, provider = project_responses(config(), transport)
    with pytest.raises(ProjectDataError):
        provider.fetch_snapshot()
    database.close()


def reader_thread(
    service: ProjectSnapshotService, results: list[ProjectSnapshot]
) -> threading.Thread:
    def read() -> None:
        results.append(service.get_snapshot())

    return threading.Thread(target=read)


def test_refresh_joins_an_in_flight_request() -> None:
    database = SnapshotDatabase(":memory:")
    provider = BlockingProvider()
    service = ProjectSnapshotService(provider, database, minimum_refresh_seconds=0)
    results: list[ProjectSnapshot] = []
    first = reader_thread(service, results)
    second = reader_thread(service, results)

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


def test_project_snapshot_skips_unsupported_item_content() -> None:
    field = status_field(option("todo", "Todo"))
    transport = FakeTransport(
        {
            PROJECT_URL: [response({"id": 2})],
            FIELDS_URL: [response({"fields": [field]})],
            ITEMS_URL: [response({"items": [{"content_type": "Unsupported"}]})],
        }
    )
    database, provider = project_responses(config(), transport)
    snapshot = provider.fetch_snapshot()
    assert snapshot.columns[-1].items == ()
    database.close()


def test_fetch_held_item_uses_cached_status_when_rate_limited() -> None:
    provider = LimitedProvider()
    database = SnapshotDatabase(":memory:")
    service = ProjectSnapshotService(
        provider,
        database,
        minimum_refresh_seconds=60,
        clock=lambda: datetime(2026, 9, 29, 10, tzinfo=UTC),
    )

    held, status = service.fetch_held_item("item-1")

    assert held.item_key == "item-1"
    assert status == "Todo"
    assert provider.fetch_item_args == ("item-1", None, (), {})
    database.close()


@pytest.mark.parametrize(
    ("snapshot", "expected"),
    [
        (ProjectSnapshot(None, None, REFRESH_COLUMNS), True),
        (
            ProjectSnapshot(
                "2026-09-29T09:59:30+00:00",
                "2026-09-29T11:00:00+00:00",
                REFRESH_COLUMNS,
            ),
            False,
        ),
        (ProjectSnapshot("invalid", None, REFRESH_COLUMNS), True),
        (
            ProjectSnapshot(
                "2026-09-29T09:59:30+00:00",
                "invalid",
                REFRESH_COLUMNS,
            ),
            True,
        ),
    ],
    ids=[
        "never-fetched",
        "recent-with-future-limit",
        "invalid-fetched-at",
        "invalid-limit",
    ],
)
def test_refresh_due_rules(snapshot: ProjectSnapshot, expected: bool) -> None:
    database = SnapshotDatabase(":memory:")
    service = ProjectSnapshotService(
        BlankProvider(),
        database,
        minimum_refresh_seconds=60,
        clock=lambda: datetime(2026, 9, 29, 10, tzinfo=UTC),
    )

    assert service._refresh_due(snapshot) is expected
    database.close()
