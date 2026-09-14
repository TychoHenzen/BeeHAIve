from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from beehaiive.pbi_refinement_mutation import (
    REFINEMENT_SECTION_ORDER,
    PbiRefinementMutationError,
    PbiRefinementUpdateRequest,
)
from beehaiive.provider import GitHubProjectProvider


class FakeRefinementGraphQLClient:
    def __init__(self, *, sub_issues: list[dict[str, object]] | None = None) -> None:
        self.project_status = "Backlog"
        self.sub_issues = sub_issues or []
        self.fail_status_once = False
        self.repeat_cursor_for: str | None = None
        self.malformed_connection_for: tuple[str, str] | None = None
        self.writes: list[str] = []
        self.labels = {
            "old-priority": ("Prio 4 - Normal", "Priority 4"),
            "old-effort": ("Effort 3 - Small", "Effort 3"),
            "priority-5": ("Prio 5 - Planned", "Priority 5"),
            "effort-5": ("Effort 5 - Large", "Effort 5"),
            "effort-13": ("Effort 13 - Epic", "Effort 13"),
            "effort-13-candidate": ("Effort 13 - Candidate", "Custom label"),
            "enhancement": ("enhancement", "Enhancement"),
            "keep": ("documentation", ""),
        }
        self.issue: dict[str, Any] = {
            "id": "issue-node",
            "number": 65,
            "url": "https://github.com/owner/repo/issues/65",
            "body": "Preserve this preamble.\n\n## Notes\nKeep this note.\n",
            "state": "OPEN",
            "labels": [
                {"name": "Prio 4 - Normal"},
                {"name": "Effort 3 - Small"},
                {"name": "Effort 13 - Candidate"},
                {"name": "enhancement"},
                {"name": "documentation"},
            ],
        }

    @staticmethod
    def _page(nodes: list[dict[str, object]]) -> dict[str, object]:
        return {
            "nodes": nodes,
            "pageInfo": {"hasNextPage": False, "endCursor": None},
        }

    def _connection(
        self, nodes: list[dict[str, object]], name: str
    ) -> dict[str, object]:
        connection = self._page(nodes)
        if self.malformed_connection_for is not None:
            malformed_name, missing_field = self.malformed_connection_for
            if malformed_name == name:
                connection.pop(missing_field, None)
        if self.repeat_cursor_for == name:
            connection["pageInfo"] = {"hasNextPage": True, "endCursor": "repeat"}
        return connection

    def _project(self, *, include_items: bool) -> dict[str, object]:
        options = [
            {"id": f"{name.casefold()}-option", "name": name}
            for name in ("Backlog", "Todo", "In Progress", "Done")
        ]
        project: dict[str, object] = {
            "id": "project-node",
            "repositories": self._connection(
                [{"nameWithOwner": "owner/repo"}], "target_repositories"
            ),
            "fields": self._page(
                [{"id": "status-field", "name": "Status", "options": options}]
            ),
        }
        if include_items:
            project["items"] = self._connection(
                [
                    {
                        "id": "item-node",
                        "content": {
                            "__typename": "Issue",
                            "id": "issue-node",
                            "number": 65,
                            "repository": {"nameWithOwner": "owner/repo"},
                        },
                        "fieldValues": self._page(
                            [
                                {
                                    "name": self.project_status,
                                    "optionId": self.project_status.casefold()
                                    + "-option",
                                    "field": {"id": "status-field", "name": "Status"},
                                }
                            ]
                        ),
                    }
                ],
                "project_items",
            )
        return project

    def _issue_response(self) -> dict[str, object]:
        return {
            "repository": {
                "issue": {
                    **self.issue,
                    "labels": self._connection(
                        list(self.issue["labels"]), "issue_labels"
                    ),
                    "subIssues": self._connection(self.sub_issues, "issue_sub_issues"),
                }
            }
        }

    def execute(self, query: str, variables: Mapping[str, object]) -> Mapping[str, Any]:
        if "query PbiRefinementIssue" in query:
            return self._issue_response()
        if "issue(number: $number)" in query and "labels(first: 100, after:" in query:
            return {
                "repository": {
                    "issue": {
                        "labels": self._connection(
                            list(self.issue["labels"]), "issue_labels"
                        )
                    }
                }
            }
        if "subIssues(first: 100, after:" in query:
            return {
                "repository": {
                    "issue": {
                        "subIssues": self._connection(
                            self.sub_issues, "issue_sub_issues"
                        )
                    }
                }
            }
        if "repositories(first: 100" in query:
            return {"user": {"projectV2": self._project(include_items=False)}}
        if "labels(first: 100, after: $cursor)" in query:
            return {
                "repository": {
                    "id": "repository-node",
                    "nameWithOwner": "owner/repo",
                    "labels": self._connection(
                        [
                            {"id": key, "name": value[0], "description": value[1]}
                            for key, value in self.labels.items()
                        ],
                        "repository_labels",
                    ),
                }
            }
        if "items(first: 100" in query:
            return {"user": {"projectV2": self._project(include_items=True)}}
        if "mutation UpdatePbiRefinementIssue" in query:
            update = variables["input"]
            assert isinstance(update, Mapping)
            self.issue["body"] = update["body"]
            self.issue["labels"] = [
                {"name": self.labels[label_id][0]} for label_id in update["labelIds"]
            ]
            self.writes.append("issue")
            return {"updateIssue": {"issue": {"id": "issue-node"}}}
        if "mutation($input: UpdateProjectV2ItemFieldValueInput!)" in query:
            self.writes.append("project_status")
            if self.fail_status_once:
                self.fail_status_once = False
                raise RuntimeError("simulated status write failure")
            update = variables["input"]
            assert isinstance(update, Mapping)
            option_id = update["value"]["singleSelectOptionId"]
            self.project_status = "Todo" if option_id == "todo-option" else "Backlog"
            return {
                "updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "item-node"}}
            }
        raise AssertionError(f"Unexpected GraphQL operation: {query}")


def _request(*, effort: str = "Effort 5 - Large") -> PbiRefinementUpdateRequest:
    return PbiRefinementUpdateRequest(
        project_id="owner:2",
        repository="owner/repo",
        pbi_number=65,
        sections={name: f"Refined {name}." for name in REFINEMENT_SECTION_ORDER},
        priority_label="Prio 5 - Planned",
        effort_label=effort,
        standard_labels=("enhancement",),
    )


def _provider(client: FakeRefinementGraphQLClient) -> GitHubProjectProvider:
    return GitHubProjectProvider(
        owner="owner", project_number=2, token="test-token", client=client
    )


def test_apply_refinement_preserves_text_replaces_labels_and_retries() -> None:
    client = FakeRefinementGraphQLClient()
    provider = _provider(client)

    result = provider.apply_pbi_refinement(_request())

    assert result.status == "complete"
    assert result.project_status == "Todo"
    assert set(result.labels) == {
        "Prio 5 - Planned",
        "Effort 5 - Large",
        "Effort 13 - Candidate",
        "enhancement",
        "documentation",
    }
    assert client.issue["body"].startswith(
        "Preserve this preamble.\n\n## Notes\nKeep this note.\n"
    )
    assert (
        "## Acceptance criteria\nRefined Acceptance criteria." in client.issue["body"]
    )
    assert client.writes == ["issue", "project_status"]

    replay = provider.apply_pbi_refinement(_request())

    assert replay.status == "complete"
    assert client.writes == ["issue", "project_status"]


def test_effort_13_requires_split_evidence_and_stays_in_backlog() -> None:
    client = FakeRefinementGraphQLClient()
    provider = _provider(client)

    with pytest.raises(PbiRefinementMutationError) as error:
        provider.apply_pbi_refinement(_request(effort="Effort 13 - Epic"))
    assert error.value.code == "missing_split_evidence"
    assert client.writes == []

    client.sub_issues = [{"number": 66, "title": "Child PBI", "state": "OPEN"}]
    result = provider.apply_pbi_refinement(_request(effort="Effort 13 - Epic"))

    assert result.status == "complete"
    assert result.project_status == "Backlog"
    assert [issue["number"] for issue in result.linked_sub_issues] == [66]
    assert client.writes == ["issue"]


def test_partial_status_failure_is_reported_and_retry_only_finishes_pending_step() -> (
    None
):
    client = FakeRefinementGraphQLClient()
    client.fail_status_once = True
    provider = _provider(client)

    partial = provider.apply_pbi_refinement(_request())

    assert partial.status == "partial"
    assert partial.completed_steps == ("issue_body_and_labels",)
    assert partial.pending_step == "project_status"
    assert client.project_status == "Backlog"

    completed = provider.apply_pbi_refinement(_request())

    assert completed.status == "complete"
    assert completed.project_status == "Todo"
    assert client.writes.count("issue") == 1
    assert client.writes.count("project_status") == 2


def test_duplicate_managed_section_fails_before_any_github_write() -> None:
    client = FakeRefinementGraphQLClient()
    client.issue["body"] = "## Outcome\nFirst\n\n## Outcome\nSecond\n"

    with pytest.raises(PbiRefinementMutationError) as error:
        _provider(client).apply_pbi_refinement(_request())

    assert error.value.code == "duplicate_section_heading"
    assert client.writes == []


def test_empty_github_issue_body_can_be_refined() -> None:
    client = FakeRefinementGraphQLClient()
    client.issue["body"] = None

    result = _provider(client).apply_pbi_refinement(_request())

    assert result.status == "complete"
    assert "## Outcome\nRefined Outcome." in client.issue["body"]


def test_repeated_github_pagination_cursors_return_bounded_preflight_failure() -> None:
    for connection in (
        "target_repositories",
        "repository_labels",
        "project_items",
        "issue_labels",
        "issue_sub_issues",
    ):
        client = FakeRefinementGraphQLClient()
        client.repeat_cursor_for = connection

        with pytest.raises(PbiRefinementMutationError) as error:
            _provider(client).apply_pbi_refinement(_request())

        assert error.value.code == "preflight_failed"
        assert client.writes == []


@pytest.mark.parametrize(
    ("connection", "missing_field"),
    [
        ("issue_labels", "nodes"),
        ("issue_labels", "pageInfo"),
        ("issue_sub_issues", "nodes"),
        ("issue_sub_issues", "pageInfo"),
        ("project_items", "nodes"),
        ("project_items", "pageInfo"),
    ],
)
def test_incomplete_issue_connections_fail_preflight_without_writes(
    connection: str, missing_field: str
) -> None:
    client = FakeRefinementGraphQLClient()
    client.malformed_connection_for = (connection, missing_field)

    with pytest.raises(PbiRefinementMutationError) as error:
        _provider(client).apply_pbi_refinement(_request())

    assert error.value.code == "preflight_failed"
    assert client.writes == []
