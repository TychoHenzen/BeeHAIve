from typing import Any

from beehaiive.provider import (
    ProviderError,
)


class CheckGraphQLClient:
    def __init__(
        self,
        *,
        observed_head: str | None = "head-1",
        rollup_oid: str | None = None,
        page_observed_head: str | None = None,
        page_rollup_oid: str | None = None,
        observed_state: str = "OPEN",
        error: str | None = None,
    ) -> None:
        self.cursors: list[object] = []
        self.observed_head = observed_head
        self.rollup_oid = rollup_oid
        self.page_observed_head = page_observed_head
        self.page_rollup_oid = page_rollup_oid
        self.observed_state = observed_state
        self.error = error

    def execute(self, query: str, variables: dict[str, object]) -> dict[str, Any]:
        if "statusCheckRollup" in query:
            if self.error is not None:
                raise ProviderError(self.error)
            cursor = variables.get("cursor")
            self.cursors.append(cursor)
            page_head = (
                self.page_observed_head
                if cursor is not None and self.page_observed_head is not None
                else self.observed_head
            )
            page_rollup_oid = (
                self.page_rollup_oid
                if cursor is not None and self.page_rollup_oid is not None
                else self.rollup_oid
            )
            contexts = (
                [
                    {
                        "__typename": "CheckRun",
                        "name": "python-tests",
                        "status": "COMPLETED",
                        "conclusion": "SUCCESS",
                        "isRequired": True,
                        "detailsUrl": "https://example.test/python-tests",
                        "completedAt": "2026-09-11T08:01:00Z",
                    }
                ]
                if cursor is None
                else [
                    {
                        "__typename": "StatusContext",
                        "context": "legacy-status",
                        "state": "SUCCESS",
                        "isRequired": False,
                        "targetUrl": "https://example.test/legacy-status",
                        "updatedAt": "2026-09-11T08:02:00Z",
                    }
                ]
            )
            return {
                "repository": {
                    "pullRequest": {
                        "state": self.observed_state,
                        "headRef": (
                            {"target": {"oid": page_head}}
                            if page_head is not None
                            else None
                        ),
                        "statusCheckRollup": {
                            "commit": {"oid": page_rollup_oid or page_head},
                            "state": "SUCCESS",
                            "contexts": {
                                "nodes": contexts,
                                "pageInfo": {
                                    "hasNextPage": cursor is None,
                                    "endCursor": "checks-1" if cursor is None else None,
                                },
                            },
                        },
                    }
                }
            }
        return {
            "repository": {
                "issue": {
                    "labels": {
                        "nodes": [],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    },
                    "subIssues": {
                        "nodes": [],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    },
                    "comments": {
                        "nodes": [],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    },
                    "closedByPullRequestsReferences": {
                        "nodes": [],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    },
                },
                "pullRequest": {
                    "reviewRequests": {
                        "nodes": [],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    },
                    "latestReviews": {
                        "nodes": [],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    },
                },
            }
        }
