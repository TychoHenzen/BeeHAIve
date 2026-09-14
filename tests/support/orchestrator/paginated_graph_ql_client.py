from typing import Any


class PaginatedGraphQLClient:
    def __init__(self) -> None:
        self.repositories = [
            {"nameWithOwner": f"owner/repo-{index:03d}"} for index in range(101)
        ]
        self.items = [
            {
                "content": {
                    "__typename": "Issue",
                    "number": index,
                    "title": f"PBI {index}",
                    "repository": {"nameWithOwner": "owner/repo-000"},
                },
                "fieldValues": {
                    "nodes": [
                        {},
                        {"name": "Backlog", "field": {"name": "Status"}},
                    ]
                },
            }
            for index in (1, 2)
        ]

    def execute(self, query: str, variables: dict[str, object]) -> dict[str, Any]:
        cursor = variables.get("cursor")
        if "repositories" in query:
            nodes = (
                self.repositories[:100] if cursor is None else self.repositories[100:]
            )
            return {
                "user": {
                    "projectV2": {
                        "repositories": {
                            "nodes": nodes,
                            "pageInfo": {
                                "hasNextPage": cursor is None,
                                "endCursor": "repos-1" if cursor is None else None,
                            },
                        }
                    }
                }
            }
        if "items" in query:
            nodes = self.items[:1] if cursor is None else self.items[1:]
            return {
                "user": {
                    "projectV2": {
                        "items": {
                            "nodes": nodes,
                            "pageInfo": {
                                "hasNextPage": cursor is None,
                                "endCursor": "items-1" if cursor is None else None,
                            },
                        }
                    }
                }
            }
        return {"user": {"projectV2": {"title": "Paged Planning"}}}
