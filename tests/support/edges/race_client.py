from __future__ import annotations

from typing import Any

from beehaiive.provider import (
    ProviderError,
)
from tests.support.edges.error_handoff_client import ErrorHandoffClient


class RaceClient(ErrorHandoffClient):
    def __init__(self, mode: str) -> None:
        super().__init__()
        self.mode = mode
        self.repository_calls = 0

    def execute(self, query: str, variables: dict[str, object]) -> dict[str, Any]:
        if "pullRequests" in query:
            self.repository_calls += 1
            if self.repository_calls > 1 and self.mode in {
                "ref-query-error",
                "pr-query-error",
            }:
                raise ProviderError("recheck failed")
            ref = (
                None
                if self.mode.startswith("ref-")
                else {
                    "name": variables["qualifiedBranch"],
                    "target": {"oid": "oid"},
                }
            )
            return {
                "repository": {
                    "id": "repo-id",
                    "defaultBranchRef": {
                        "name": "main",
                        "target": {"oid": "oid"},
                    },
                    "ref": ref,
                    "pullRequests": {
                        "nodes": [],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    },
                }
            }
        if "CreateRefInput" in query:
            raise ProviderError("reference already exists")
        if "CreatePullRequestInput" in query:
            raise ProviderError("pull request already exists")
        return super().execute(query, variables)
