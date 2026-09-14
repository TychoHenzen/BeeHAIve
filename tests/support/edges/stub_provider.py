from __future__ import annotations

from pathlib import Path

from beehaiive.models import (
    HandoffRequest,
    HandoffResult,
    ProjectSnapshot,
    PullRequestSnapshot,
)


class StubProvider:
    def __init__(self) -> None:
        self.cache_invalidated = False

    def discover_project(self, project_id: str) -> ProjectSnapshot:
        return ProjectSnapshot(project_id, "Planning", ())

    def invalidate_discovery_cache(self) -> None:
        self.cache_invalidated = True

    def create_handoff(self, request: HandoffRequest) -> HandoffResult:
        return HandoffResult(request.branch, "https://example.test/pull/1", 1)

    def resolve_base_branch(self, repository: str, requested: str | None) -> str:
        return requested or "main"

    def validate_handoff(
        self, repository: str, branch: str, requested_base: str | None
    ) -> str:
        return self.resolve_base_branch(repository, requested_base)

    def get_pull_request(self, repository: str, number: int) -> PullRequestSnapshot:
        return PullRequestSnapshot(
            repository,
            number,
            "PR_1",
            "https://example.test/pull/1",
            "OPEN",
            False,
            "feature",
            "head",
            "main",
            "base",
            "MERGEABLE",
            "CLEAN",
        )

    def update_source_branch(
        self,
        snapshot: PullRequestSnapshot,
        worktree: str | Path,
        expected_head: str,
        repaired_head: str,
    ) -> None:
        del snapshot, worktree, expected_head, repaired_head
