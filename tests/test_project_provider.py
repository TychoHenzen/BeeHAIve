from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from beehaiive.config import CoreConfig, CoreConfigurationError
from beehaiive.database import SnapshotDatabase
from beehaiive.models import ProjectCard, ProjectColumn, ProjectSnapshot
from beehaiive.project import (
    ProjectDataError,
    parse_closing_issue_numbers,
)
from beehaiive.rest import (
    RateLimitError,
)
from beehaiive.snapshot import ProjectSnapshotService
from tests.support import FakeTransport, config, project_responses, response


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
