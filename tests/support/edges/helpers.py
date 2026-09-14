from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from beehaiive.models import (
    HandoffRequest,
    PbiSnapshot,
    ProjectSnapshot,
    RepositorySnapshot,
)


def project_data(
    *, title: object = "Planning", items: list[object] | None = None
) -> dict[str, Any]:
    return {
        "user": {
            "projectV2": {
                "title": title,
                "repositories": {
                    "nodes": [{"nameWithOwner": "owner/api"}],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                },
                "items": {
                    "nodes": items or [],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                },
            }
        }
    }


def handoff_request(*, base_branch: str | None = None) -> HandoffRequest:
    return HandoffRequest(
        project_id="owner:7",
        repository="owner/api",
        pbi_number=1,
        title="API one",
        branch="codex/api-1",
        base_branch=base_branch,
        body="Closes #1",
        run_id="run-1",
    )


def git_command(cwd: Path, *arguments: str) -> str:
    result = subprocess.run(
        ("git", *arguments),
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return result.stdout.strip()


def pull_request_data(
    *,
    mergeable: object = "CONFLICTING",
    merge_state: object = "DIRTY",
    source_name: object = "feature",
    source_ref_name: object = "feature",
    source_head: object = "source-head",
    target_name: object = "main",
    target_ref_name: object = "main",
    target_head: object = "target-head",
    pull_request: object = "present",
) -> dict[str, Any]:
    if pull_request is None:
        return {"repository": {"pullRequest": None}}
    return {
        "repository": {
            "pullRequest": {
                "id": "PR_1",
                "number": 1,
                "url": "https://example.test/pull/1",
                "state": "OPEN",
                "merged": False,
                "headRefName": source_name,
                "headRef": {
                    "name": source_ref_name,
                    "target": {"oid": source_head},
                },
                "baseRefName": target_name,
                "baseRef": {
                    "name": target_ref_name,
                    "target": {"oid": target_head},
                },
                "mergeable": mergeable,
                "mergeStateStatus": merge_state,
            }
        }
    }


def storage_snapshot() -> ProjectSnapshot:
    return ProjectSnapshot(
        "project-1",
        "Planning",
        (RepositorySnapshot("owner/api", (PbiSnapshot("owner/api", 1, "one"),)),),
    )
