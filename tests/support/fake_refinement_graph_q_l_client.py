from __future__ import annotations

from collections.abc import Mapping
from typing import Any


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
