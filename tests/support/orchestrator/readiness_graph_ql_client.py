from typing import Any

from tests.support.orchestrator.fake_graph_ql_client import FakeGraphQLClient
from tests.support.orchestrator.helpers import (
    readiness_connection as _readiness_connection,
)
from tests.support.orchestrator.helpers import (
    readiness_issue_content as _readiness_issue_content,
)


class ReadinessGraphQLClient(FakeGraphQLClient):
    def __init__(
        self,
        children: list[dict[str, Any]],
        *,
        dependency_pages: dict[int, list[list[dict[str, Any]]]] | None = None,
        rest_statuses: dict[int, int] | None = None,
        extra_subissues: list[dict[str, Any]] | None = None,
    ) -> None:
        child_nodes = [
            {
                "number": child["number"],
                "title": child["title"],
                "state": child["issue_state"],
                "stateReason": child["state_reason"],
                "labels": _readiness_connection([]),
            }
            for child in children
        ]
        child_nodes.extend(extra_subissues or [])
        items = [
            {
                "content": _readiness_issue_content(
                    1, "Parent PBI", "OPEN", None, child_nodes
                ),
                "fieldValues": {
                    "nodes": [{"name": "In Progress", "field": {"name": "Status"}}]
                },
            }
        ]
        for child in children:
            project_status = child["project_status"]
            values = (
                [{"name": project_status, "field": {"name": "Status"}}]
                if isinstance(project_status, str)
                else []
            )
            items.append(
                {
                    "content": _readiness_issue_content(
                        child["number"],
                        child["title"],
                        child["issue_state"],
                        child["state_reason"],
                    ),
                    "fieldValues": {"nodes": values},
                }
            )
        super().__init__(
            {
                "user": {
                    "projectV2": {
                        "title": "Planning",
                        "repositories": {
                            "nodes": [{"nameWithOwner": "owner/api"}],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        },
                        "items": {
                            "nodes": items,
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        },
                    }
                }
            }
        )
        self.dependency_pages = dependency_pages or {}
        self.rest_statuses = rest_statuses or {}
        self.rest_calls: list[tuple[str, str]] = []
        self.queries: list[str] = []

    def execute(self, query: str, variables: dict[str, object]) -> dict[str, Any]:
        self.queries.append(query)
        return super().execute(query, variables)

    def request_rest(
        self, method: str, path: str, payload: dict[str, object] | None = None
    ) -> tuple[int, list[dict[str, Any]]]:
        self.rest_calls.append((method, path))
        issue_number = int(path.split("/issues/", 1)[1].split("/", 1)[0])
        page = int(path.rsplit("page=", 1)[1])
        pages = self.dependency_pages.get(issue_number, [[]])
        response = pages[page - 1] if page <= len(pages) else []
        return self.rest_statuses.get(issue_number, 200), response
