from __future__ import annotations

from typing import Any


def build_connection(
    nodes: list[dict[str, Any]], has_next: bool = False, cursor: str | None = None
) -> dict[str, Any]:
    return {
        "nodes": nodes,
        "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
    }


def build_review(identifier: str, actor_type: str, state: str) -> dict[str, Any]:
    return {
        "id": identifier,
        "author": {
            "__typename": actor_type,
            "id": f"{identifier}-author",
            "login": f"{actor_type.lower()}-reviewer",
        },
        "state": state,
        "body": f"Review body {identifier}",
        "submittedAt": "2026-09-13T10:00:00Z",
        "url": f"https://github.com/owner/repo/pull/7#pullrequestreview-{identifier}",
    }


def build_comment(identifier: str, actor_type: str, body: str) -> dict[str, Any]:
    return {
        "id": identifier,
        "author": {
            "__typename": actor_type,
            "id": f"{identifier}-author",
            "login": f"{actor_type.lower()}-commenter",
        },
        "body": body,
        "createdAt": "2026-09-13T10:01:00Z",
        "updatedAt": "2026-09-13T10:02:00Z",
        "url": f"https://github.com/owner/repo/pull/7#discussion-{identifier}",
    }


def build_thread(identifier: str, comments: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": identifier,
        "path": "src/app.py",
        "line": 22,
        "originalLine": 20,
        "startLine": 18,
        "originalStartLine": 16,
        "diffSide": "RIGHT",
        "startDiffSide": "RIGHT",
        "isResolved": identifier == "THREAD-2",
        "isOutdated": identifier == "THREAD-1",
        "comments": comments,
    }
