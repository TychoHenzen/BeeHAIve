from typing import Any


class NestedMetadataGraphQLClient:
    def __init__(self) -> None:
        self.repositories = [{"nameWithOwner": "owner/api"}]
        self.subtasks = [
            {
                "number": number,
                "title": f"Child {number}",
                "state": "OPEN",
                "labels": {
                    "nodes": [],
                    "pageInfo": {"hasNextPage": number == 1, "endCursor": "labels-2"},
                },
            }
            for number in range(1, 101)
        ]
        self.comments = [
            {
                "author": {"login": "writer"},
                "body": f"Comment {number}",
                "createdAt": f"2026-09-09T08:{number:02d}:00Z",
                "url": f"https://example.test/comment/{number}",
            }
            for number in range(1, 102)
        ]
        self.pull_requests = [
            {
                "number": number,
                "url": f"https://example.test/pull/{number}",
                "reviewDecision": "APPROVED",
                "reviewRequests": {
                    "nodes": (
                        [{"requestedReviewer": {"login": "reviewer-1"}}]
                        if number == 1
                        else []
                    ),
                    "pageInfo": {
                        "hasNextPage": number == 1,
                        "endCursor": "requests-1" if number == 1 else None,
                    },
                },
                "latestReviews": {
                    "nodes": (
                        [{"author": {"login": "reviewer-1"}, "state": "COMMENTED"}]
                        if number == 1
                        else []
                    ),
                    "pageInfo": {
                        "hasNextPage": number == 1,
                        "endCursor": "reviews-1" if number == 1 else None,
                    },
                },
            }
            for number in range(1, 102)
        ]

    def execute(self, query: str, variables: dict[str, object]) -> dict[str, Any]:
        cursor = variables.get("cursor")
        if "projectV2" in query and "items" in query:
            issue = {
                "__typename": "Issue",
                "number": 1,
                "title": "Paged issue",
                "repository": {"nameWithOwner": "owner/api"},
                "labels": {
                    "nodes": [{"name": "escalation/terra"}],
                    "pageInfo": {"hasNextPage": True, "endCursor": "labels-1"},
                },
                "subIssues": {
                    "nodes": self.subtasks,
                    "pageInfo": {"hasNextPage": True, "endCursor": "subissues-1"},
                },
                "comments": {
                    "nodes": self.comments[:100],
                    "pageInfo": {"hasNextPage": True, "endCursor": "comments-1"},
                },
                "closedByPullRequestsReferences": {
                    "nodes": self.pull_requests[:100],
                    "pageInfo": {"hasNextPage": True, "endCursor": "pulls-1"},
                },
            }
            return {
                "user": {
                    "projectV2": {
                        "items": {
                            "nodes": [
                                {
                                    "content": issue,
                                    "fieldValues": {
                                        "nodes": [
                                            {
                                                "name": "Backlog",
                                                "field": {"name": "Status"},
                                            }
                                        ]
                                    },
                                }
                            ],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                }
            }
        if "repositories" in query:
            nodes = self.repositories if cursor is None else []
            return {
                "user": {
                    "projectV2": {
                        "repositories": {
                            "nodes": nodes,
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                }
            }
        if "closedByPullRequestsReferences" in query:
            nodes = self.pull_requests[100:] if cursor == "pulls-1" else []
            return {
                "repository": {
                    "issue": {
                        "closedByPullRequestsReferences": {
                            "nodes": nodes,
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                }
            }
        if "subIssues" in query:
            return {
                "repository": {
                    "issue": {
                        "subIssues": {
                            "nodes": [
                                {
                                    "number": 101,
                                    "title": "Child 101",
                                    "state": "OPEN",
                                    "labels": {
                                        "nodes": [],
                                        "pageInfo": {
                                            "hasNextPage": False,
                                            "endCursor": None,
                                        },
                                    },
                                }
                            ],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                }
            }
        if "comments" in query:
            return {
                "repository": {
                    "issue": {
                        "comments": {
                            "nodes": [self.comments[100]],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                }
            }
        if "reviewRequests" in query:
            return {
                "repository": {
                    "pullRequest": {
                        "reviewRequests": {
                            "nodes": [{"requestedReviewer": {"login": "reviewer-101"}}],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                }
            }
        if "latestReviews" in query:
            return {
                "repository": {
                    "pullRequest": {
                        "latestReviews": {
                            "nodes": [
                                {
                                    "author": {"login": "reviewer-101"},
                                    "state": "APPROVED",
                                }
                            ],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                }
            }
        if "labels" in query:
            labels = (
                [{"name": "bounces/2"}]
                if variables.get("number") == 1 and cursor == "labels-1"
                else [{"name": "stage/implement"}]
                if variables.get("number") == 1 and cursor == "labels-2"
                else []
            )
            return {
                "repository": {
                    "issue": {
                        "labels": {
                            "nodes": labels,
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                }
            }
        return {"user": {"projectV2": {"title": "Paged Planning"}}}
