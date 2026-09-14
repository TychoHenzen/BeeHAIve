from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from beehaiive.models import (
    HandoffRequest,
    HandoffResult,
    ProjectSnapshot,
    PullRequestSnapshot,
)


class ProjectProvider(Protocol):
    """Provider used by the orchestrator for discovery and handoff."""

    def discover_project(self, project_id: str) -> ProjectSnapshot:
        """Return the selected project and every linked repository."""

        ...

    def create_handoff(self, request: HandoffRequest) -> HandoffResult:
        """Create or reuse the branch and pull request for a PBI run."""

        ...

    def resolve_base_branch(self, repository: str, requested: str | None) -> str:
        """Resolve and return the base branch before a handoff is persisted."""

        ...

    def validate_handoff(
        self, repository: str, branch: str, requested_base: str | None
    ) -> str:
        """Validate handoff names and return the existing base branch."""

        ...

    def get_pull_request(self, repository: str, number: int) -> PullRequestSnapshot:
        """Return current pull-request identity and mergeability evidence."""

        ...

    def complete_approved_handoff(
        self,
        request: HandoffRequest,
        pull_request_number: int,
        pull_request_url: str,
        expected_head: str,
        authorization: Mapping[str, object] | None,
    ) -> Mapping[str, object]:
        """Merge one authorized PR and reconcile its persisted completion."""

        ...

    def update_source_branch(
        self,
        snapshot: PullRequestSnapshot,
        worktree: str | Path,
        expected_head: str,
        repaired_head: str,
    ) -> None:
        """Update only the existing source branch with an expected-head guard."""

        ...


__all__ = ["ProjectProvider"]
