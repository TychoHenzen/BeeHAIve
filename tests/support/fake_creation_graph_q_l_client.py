from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class FakeCreationGraphQLClient:
    def __init__(
        self,
        labels: tuple[str, ...] = ("enhancement",),
        *,
        linked_repositories: tuple[str, ...] = ("owner/repo",),
        fail_after_step: str | None = None,
    ) -> None:
        self.labels = labels
        self.linked_repositories = linked_repositories
        self.fail_after_step = fail_after_step
        self.failure_emitted = False
        self.issue: dict[str, Any] | None = None
        self.items: list[dict[str, Any]] = []
        self.other_items: list[dict[str, Any]] = [
            {
                "id": "draft-item-id",
                "content": {"__typename": "DraftIssue"},
                "fieldValues": {"nodes": []},
            },
            {"id": "deleted-content-item-id", "content": None},
        ]
        self.mutations: list[tuple[str, Mapping[str, object]]] = []
        self.rest_calls: list[tuple[str, str, Mapping[str, object] | None]] = []

    def execute(self, query: str, variables: Mapping[str, object]) -> Mapping[str, Any]:
        if "addLabelsToLabelable" in query:
            self.mutations.append((query, variables))
            return {"addLabelsToLabelable": {}}
        if "addProjectV2ItemById" in query:
            self.mutations.append((query, variables))
            self.items.append(
                {
                    "id": "project-item-id",
                    "content": {
                        "__typename": "Issue",
                        "id": "issue-node-id",
                        "number": 37,
                        "repository": {"nameWithOwner": "owner/repo"},
                    },
                    "fieldValues": {"nodes": [{}, {}, {}]},
                }
            )
            self._fail_once("project")
            return {"addProjectV2ItemById": {"item": {"id": "project-item-id"}}}
        if "updateProjectV2ItemFieldValue" in query:
            self.mutations.append((query, variables))
            self.items[0]["fieldValues"] = {
                "nodes": [
                    {
                        "name": "Backlog",
                        "optionId": "backlog-option",
                        "field": {"id": "status-field", "name": "Status"},
                    }
                ]
            }
            self._fail_once("backlog")
            return {
                "updateProjectV2ItemFieldValue": {
                    "projectV2Item": {"id": "project-item-id"}
                }
            }
        if "repositories(first: 100" in query:
            return self._project_data(
                repositories={
                    "nodes": [
                        {"nameWithOwner": name} for name in self.linked_repositories
                    ],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            )
        if "labels(first: 100, after:" in query:
            return {
                "repository": {
                    "id": "repository-id",
                    "nameWithOwner": "owner/repo",
                    "labels": {
                        "nodes": [
                            {"id": f"label-{name}", "name": name}
                            for name in self.labels
                        ],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    },
                }
            }
        if "issue(number: $number)" in query:
            self._fail_once("labels")
            return {"repository": {"issue": self.issue}}
        if "items(first: 100, after:" in query:
            return self._project_data(
                items={
                    "nodes": [*self.other_items, *self.items],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            )
        raise AssertionError(f"Unexpected GraphQL query: {query}")

    def _fail_once(self, step: str) -> None:
        if self.fail_after_step == step and not self.failure_emitted:
            self.failure_emitted = True
            raise RuntimeError("private-provider-detail")

    def request_rest(
        self, method: str, path: str, payload: Mapping[str, object] | None
    ) -> tuple[int, Mapping[str, Any]]:
        self.rest_calls.append((method, path, payload))
        if method != "POST" or payload is None:
            raise AssertionError("Unexpected REST request")
        self.issue = {
            "id": "issue-node-id",
            "number": 37,
            "url": "https://example.test/issues/37",
            "title": payload["title"],
            "body": payload["body"],
            "state": "OPEN",
            "labels": {"nodes": [{"name": name} for name in payload["labels"]]},
        }
        return (
            201,
            {
                "node_id": "issue-node-id",
                "number": 37,
                "html_url": "https://example.test/issues/37",
            },
        )

    @staticmethod
    def _project_data(**values: object) -> dict[str, Any]:
        status_field = {
            "id": "status-field",
            "name": "Status",
            "options": [{"id": "backlog-option", "name": "Backlog"}],
        }
        project: dict[str, Any] = {
            "id": "project-node-id",
            "fields": {"nodes": [status_field]},
        }
        project.update(values)
        return {"user": {"projectV2": project}}
