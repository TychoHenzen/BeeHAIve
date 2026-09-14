from typing import Any

from beehaiive.models import (
    PbiSnapshot,
    ProjectSnapshot,
    RepositorySnapshot,
)


def pull_request_snapshot() -> ProjectSnapshot:
    return ProjectSnapshot(
        project_id="project-1",
        name="Planning",
        repositories=(
            RepositorySnapshot(
                "owner/api",
                (
                    PbiSnapshot("owner/api", 1, "API one"),
                    PbiSnapshot("owner/api", 2, "API two"),
                ),
            ),
            RepositorySnapshot(
                "owner/web",
                (PbiSnapshot("owner/web", 3, "Web one"),),
            ),
        ),
    )


def readiness_connection(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "nodes": nodes,
        "pageInfo": {"hasNextPage": False, "endCursor": None},
    }


def readiness_issue_content(
    number: int,
    title: str,
    state: str,
    state_reason: str | None,
    subtasks: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "__typename": "Issue",
        "number": number,
        "title": title,
        "state": state,
        "stateReason": state_reason,
        "url": f"https://example.test/owner/api/issues/{number}",
        "repository": {"nameWithOwner": "owner/api"},
        "labels": readiness_connection([]),
        "subIssues": readiness_connection(subtasks or []),
        "comments": readiness_connection([]),
        "closedByPullRequestsReferences": readiness_connection([]),
    }
