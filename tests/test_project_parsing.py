from __future__ import annotations

import pytest

import beehaiive.project as project_module
from beehaiive.project import (
    ProjectDataError,
)
from tests.support import FakeTransport, config, project_responses, response


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
