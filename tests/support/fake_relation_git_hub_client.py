from __future__ import annotations


def _rest_issue(number: int, body: str = "") -> dict[str, object]:
    url = f"https://github.com/owner/repo/issues/{number}"
    return {
        "id": 100 + number,
        "node_id": f"node-{number}",
        "number": number,
        "url": f"https://api.github.com/repos/owner/repo/issues/{number}",
        "html_url": url,
        "title": f"Issue {number}",
        "state": "open",
        "body": body,
    }


class FakeRelationGitHubClient:
    def __init__(
        self,
        *,
        include_unrelated_field_value: bool = False,
        include_status: bool = True,
        duplicate_status: bool = False,
    ) -> None:
        self.rest_calls: list[tuple[str, str, object]] = []
        self.include_unrelated_field_value = include_unrelated_field_value
        self.include_status = include_status
        self.duplicate_status = duplicate_status
        self.parent_body = (
            "## Outcome\nOne outcome.\n"
            "## Scope\nOne scope.\n"
            "## Implementation notes\nOne note.\n"
            "## Acceptance criteria\nOne criterion.\n"
            "## Verification\nOne check.\n"
        )

    def execute(self, query: str, variables: dict[str, object]) -> dict[str, object]:
        del query, variables
        items = []
        for number, item_id, status in (
            (1, "parent-item", "Todo"),
            (2, "item-2", "Backlog"),
            (3, "item-3", "Backlog"),
        ):
            field_values: list[dict[str, object]] = []
            if self.include_unrelated_field_value:
                field_values.append({})
            status_value: dict[str, object] = {
                "name": status,
                "field": {"id": "status-field", "name": "Status"},
            }
            if self.include_status:
                field_values.append(status_value)
            if self.duplicate_status:
                field_values.append(status_value)
            items.append(
                {
                    "id": item_id,
                    "content": {
                        "__typename": "Issue",
                        "id": f"node-{number}",
                        "number": number,
                        "repository": {"nameWithOwner": "owner/repo"},
                    },
                    "fieldValues": {
                        "nodes": field_values,
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    },
                }
            )
        return {
            "user": {
                "projectV2": {
                    "id": "project-node",
                    "repositories": {
                        "nodes": [{"nameWithOwner": "owner/repo"}],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    },
                    "fields": {"nodes": [{"id": "status-field", "name": "Status"}]},
                    "items": {
                        "nodes": items,
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    },
                }
            }
        }

    def request_rest(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
    ) -> tuple[int, dict[str, object] | list[object]]:
        self.rest_calls.append((method, path, payload))
        if path.endswith("/parent"):
            return 404, {}
        if path.endswith("/sub_issues?per_page=100&page=1"):
            return 200, []
        if method == "GET" and "/issues/" in path:
            number = int(path.split("/issues/", maxsplit=1)[1])
            issue = _rest_issue(number, self.parent_body if number == 1 else "")
            return 200, issue
        raise AssertionError(f"Unexpected REST call: {method} {path}")
