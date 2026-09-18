from beehaiive.provider import (
    GitHubProjectProvider,
)
from tests.support.orchestrator.fake_graph_ql_client import (
    FakeGraphQLClient as FakeGraphQLClient,
)


def test_github_provider_maps_live_dashboard_metadata() -> None:
    provider = GitHubProjectProvider(
        "owner",
        7,
        "token",
        client=FakeGraphQLClient(
            {
                "user": {
                    "projectV2": {
                        "title": "Planning",
                        "repositories": {"nodes": [{"nameWithOwner": "owner/api"}]},
                        "items": {
                            "nodes": [
                                {
                                    "content": {
                                        "__typename": "Issue",
                                        "number": 1,
                                        "title": "API one",
                                        "repository": {"nameWithOwner": "owner/api"},
                                        "labels": {
                                            "nodes": [
                                                {"name": "Prio 1 - Planned"},
                                                {"name": "bounces/2"},
                                                {"name": "escalation/terra"},
                                            ]
                                        },
                                        "subIssues": {
                                            "nodes": [
                                                {"number": "bad", "title": "Ignored"},
                                                {
                                                    "number": 2,
                                                    "title": "API child",
                                                    "state": "OPEN",
                                                    "labels": {
                                                        "nodes": [
                                                            {"name": "stage/implement"}
                                                        ]
                                                    },
                                                },
                                            ]
                                        },
                                        "comments": {
                                            "nodes": [
                                                {"body": "   "},
                                                {
                                                    "author": {},
                                                    "body": "Started review",
                                                    "createdAt": "2026-09-09T08:00:00Z",
                                                    "url": "https://example.test/comment/1",
                                                },
                                            ]
                                        },
                                        "closedByPullRequestsReferences": {
                                            "nodes": [
                                                {"number": "bad"},
                                                {
                                                    "number": 9,
                                                    "url": "https://example.test/pull/9",
                                                    "state": "CLOSED",
                                                    "merged": True,
                                                    "isDraft": False,
                                                    "reviewDecision": (
                                                        "CHANGES_REQUESTED"
                                                    ),
                                                    "reviewRequests": {
                                                        "nodes": [
                                                            {"requestedReviewer": None},
                                                            {"requestedReviewer": {}},
                                                            {
                                                                "requestedReviewer": {
                                                                    "login": "tests"
                                                                }
                                                            },
                                                        ]
                                                    },
                                                    "latestReviews": {
                                                        "nodes": [
                                                            {
                                                                "author": None,
                                                                "state": "COMMENTED",
                                                            },
                                                            {
                                                                "author": {},
                                                                "state": "COMMENTED",
                                                            },
                                                            {
                                                                "author": {
                                                                    "login": "bot"
                                                                },
                                                                "state": "COMMENTED",
                                                            },
                                                            {
                                                                "author": {
                                                                    "login": "tests"
                                                                },
                                                                "state": (
                                                                    "CHANGES_REQUESTED"
                                                                ),
                                                                "body": (
                                                                    "Please add a test"
                                                                ),
                                                            },
                                                            {
                                                                "author": {
                                                                    "login": "security"
                                                                },
                                                                "state": "APPROVED",
                                                                "body": "Looks good",
                                                                "submittedAt": (
                                                                    "2026-09-09T08:01:00Z"
                                                                ),
                                                                "url": "https://example.test/review/1",
                                                            },
                                                        ]
                                                    },
                                                },
                                            ]
                                        },
                                    },
                                    "fieldValues": {
                                        "nodes": [
                                            {
                                                "name": "In Progress",
                                                "field": {"name": "Status"},
                                            }
                                        ]
                                    },
                                }
                            ]
                        },
                    }
                }
            }
        ),
    )

    discovered = provider.discover_project("owner:7")

    metadata = discovered.repositories[0].pbis[0].metadata
    assert metadata["project_priority"] == 1
    assert metadata["project_order"] == 0
    child = metadata["subtasks"][0]  # type: ignore[index]
    assert child["id"] == "#2"  # type: ignore[index]
    assert child["title"] == "API child"  # type: ignore[index]
    assert child["labels"] == ["stage/implement"]  # type: ignore[index]
    assert child["readiness"] == "unknown"  # type: ignore[index]
    assert child["readiness_reasons"] == ["child_response_incomplete"]  # type: ignore[index]
    assert metadata["dependency_readiness"]["status"] == "unknown"  # type: ignore[index]
    assert metadata["readers"] == [
        {"id": "#9:tests", "name": "tests", "pull_request": 9, "status": "fail"},
        {"id": "#9:bot", "name": "bot", "pull_request": 9, "status": "pending"},
        {"id": "#9:security", "name": "security", "pull_request": 9, "status": "pass"},
    ]
    assert metadata["reviewers"]["#9:tests"]["status"] == "fail"  # type: ignore[index]
    assert metadata["reviewers"]["#9:security"]["status"] == "pass"  # type: ignore[index]
    assert metadata["reviewers"]["#9:bot"]["status"] == "pending"  # type: ignore[index]
    pull_request = metadata["pull_requests"][0]  # type: ignore[index]
    assert pull_request["state"] == "closed"  # type: ignore[index]
    assert pull_request["merged"] is True  # type: ignore[index]
    assert pull_request["is_draft"] is False  # type: ignore[index]
    assert metadata["escalation"] == {
        "current": 2,
        "consecutive": 2,
        "current_tier": "terra",
    }
    assert metadata["activity"][0]["action"] == "Started review"  # type: ignore[index]
