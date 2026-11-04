from __future__ import annotations

from typing import Any

import pytest

import beehaiive.project as project_module
from beehaiive.project import (
    ProjectDataError,
)
from tests.support import (
    PROJECT_URL,
    FakeTransport,
    config,
    option,
    project_item,
    project_responses,
    response,
    status_field,
    status_value,
)

STATUS_FIELD = {
    "id": "status",
    "name": "Status",
    "dataType": "single-select",
    "options": [
        {"id": "todo", "name": {"raw": "Todo"}},
        {"id": "done", "name": "Done"},
    ],
}

PULL_REQUEST_CONTENT = {
    "repository_url": "https://api.github.com/repos/acme/repo",
    "number": "4",
    "title": "PR",
    "body": "Closes #7",
    "labels": [{"name": "bug"}, "urgent"],
    "html_url": "https://github.com/acme/repo/pull/4",
    "state": "open",
}
PULL_REQUEST_ITEM = {
    "id": "item-1",
    "content_type": "PullRequest",
    "content": PULL_REQUEST_CONTENT,
    "fields": [status_value("todo", None, "status")],
}


MALFORMED_STATUS_FIELDS: list[tuple[list[dict[str, Any]], str]] = [
    ([], "exactly one"),
    (
        [{"id": "status", "name": "Status", "data_type": "text", "options": []}],
        "not single-select",
    ),
    (
        [{"id": "status", "name": "Status", "data_type": "single_select"}],
        "no options",
    ),
    (
        [{"name": "Status", "data_type": "single_select", "options": []}],
        "no id",
    ),
]


@pytest.mark.parametrize(
    ("fields", "match"),
    MALFORMED_STATUS_FIELDS,
    ids=["empty", "text", "no-options", "no-id"],
)
def test_status_field_rejects_malformed_shapes(
    fields: list[dict[str, Any]], match: str
) -> None:
    with pytest.raises(ProjectDataError, match=match):
        project_module._status_field(fields)


def test_list_payload_requires_the_named_key() -> None:
    with pytest.raises(ProjectDataError, match="did not contain"):
        project_module._list_payload({}, "items")


def test_status_options_read_raw_and_plain_names() -> None:
    assert project_module._status_options(STATUS_FIELD) == ("Todo", "Done")
    assert project_module._status_option_ids(STATUS_FIELD) == {
        "todo": "Todo",
        "done": "Done",
    }


def test_normalise_item_reads_pull_request_and_draft() -> None:
    pull_request = project_module._normalise_item(PULL_REQUEST_ITEM)
    assert pull_request is not None
    assert pull_request.repository == "acme/repo"
    assert pull_request.number == 4
    assert pull_request.labels == ("bug", "urgent")
    assert pull_request.linked_issue_numbers == (7,)
    draft_item: dict[str, Any] = {
        "content_type": "DraftIssue",
        "title": "draft",
        "content": {},
    }
    draft = project_module._normalise_item(draft_item)
    assert draft is not None
    assert draft.repository is None
    assert draft.number is None
    assert project_module._normalise_item({"content_type": "Milestone"}) is None


def test_item_status_and_repository_helpers() -> None:
    plain_item = {"fields": [{"id": "other", "name": "Status", "value": "Todo"}]}
    assert project_module._item_status(plain_item, "status", ("Todo",), {}) == "Todo"
    named_fields = [
        {"id": "other", "name": "Other", "value": "ignored"},
        {"id": "status", "value": "Todo"},
    ]
    named_item = {"fields": named_fields}
    assert project_module._item_status(named_item, "status", ("Todo",), {}) == "Todo"
    assert project_module._item_status({}, "status", ("Todo",), {}) == "No status"
    assert (
        project_module._repository_name({"repo": {"name_with_owner": "a/b"}}) == "a/b"
    )
    assert project_module._repository_name({"repository_url": "https://x/a/b"}) == "a/b"
    assert project_module._labels(None) == ()
    assert project_module._optional_int(True) is None
    assert project_module._optional_int("4") == 4


def test_fetch_item_reads_a_nested_item_payload() -> None:
    field = status_field(option("todo", "Todo"))
    item = project_item(
        "Issue",
        {"repository": "acme/repo", "number": 1, "title": "Issue"},
        status_value("todo", None, "407"),
    )
    item_url = f"{PROJECT_URL}/items/item%2F1?fields=407"
    transport = FakeTransport(
        {
            f"{PROJECT_URL}/fields": [response({"fields": [field]})],
            item_url: [response({"item": item})],
        }
    )
    database, provider = project_responses(config(), transport)
    held_item = provider.fetch_item("item/1", status_field_id=None)
    assert held_item.card.repository == "acme/repo"
    assert held_item.status == "Todo"
    database.close()


def test_fetch_item_rejects_unknown_content_type() -> None:
    bad_transport = FakeTransport(
        {
            f"{PROJECT_URL}/items/bad?fields=407": [
                response({"content_type": "Unsupported"})
            ]
        }
    )
    database, provider = project_responses(config(), bad_transport)
    with pytest.raises(ProjectDataError, match="unavailable"):
        provider.fetch_item("bad", status_field_id="407", status_options=("Todo",))
    database.close()


def test_project_name_helpers_read_plain_and_nested_values() -> None:
    assert project_module._repository_name({"repository": "acme/repo"}) == "acme/repo"
    assert project_module._display_name({"raw": ""}) == ""
