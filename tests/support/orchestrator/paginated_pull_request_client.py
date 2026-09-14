from typing import Any

from tests.support.orchestrator.handoff_graph_ql_client import HandoffGraphQLClient


class PaginatedPullRequestClient(HandoffGraphQLClient):
    def __init__(self) -> None:
        super().__init__()
        self.ref_exists = True
        self.pull_requests = [
            {
                "id": f"old-node-{index}",
                "number": index,
                "url": f"https://example.test/owner/api/pull/{index}",
                "title": f"Old {index}",
                "state": "OPEN",
                "isDraft": True,
                "headRefName": f"old-{index}",
                "headRefOid": "base-oid",
                "baseRefName": "main",
                "body": "old body",
            }
            for index in range(100)
        ]
        self.pull_requests.append(
            {
                "id": "pull-request-node-101",
                "number": 101,
                "url": "https://example.test/owner/api/pull/101",
                "title": "API one",
                "state": "OPEN",
                "isDraft": True,
                "headRefName": "codex/api-1",
                "headRefOid": "base-oid",
                "baseRefName": "main",
                "body": "old body",
            }
        )

    def execute(self, query: str, variables: dict[str, object]) -> dict[str, Any]:
        if (
            "CreateRefInput" in query
            or "CreatePullRequestInput" in query
            or "UpdatePullRequestInput" in query
        ):
            return super().execute(query, variables)
        cursor = variables.get("pullRequestCursor")
        nodes = self.pull_requests[:100] if cursor is None else self.pull_requests[100:]
        return {
            "repository": {
                "id": "repo-id",
                "defaultBranchRef": {
                    "name": "main",
                    "target": {"oid": "base-oid"},
                },
                "ref": {
                    "name": variables["qualifiedBranch"],
                    "target": {"oid": "base-oid"},
                },
                "pullRequests": {
                    "nodes": nodes,
                    "pageInfo": {
                        "hasNextPage": cursor is None,
                        "endCursor": "prs-1" if cursor is None else None,
                    },
                },
            }
        }
