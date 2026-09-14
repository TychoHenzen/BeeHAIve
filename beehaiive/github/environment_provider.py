from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from pathlib import Path
from threading import Lock
from typing import cast

from beehaiive.github.constants import (
    DISCOVERY_CACHE_SECONDS_ENV as DISCOVERY_CACHE_SECONDS_ENV,
)
from beehaiive.github.project_protocol import ProjectProvider as ProjectProvider
from beehaiive.github.project_provider import (
    GitHubProjectProvider as GitHubProjectProvider,
)
from beehaiive.models import (
    HandoffRequest,
    HandoffResult,
    ProjectSnapshot,
    PullRequestSnapshot,
)
from beehaiive.pbi_creation import (
    PbiCreationProgress,
    PbiCreationProvider,
    PbiCreationRequest,
    PbiCreationResult,
    PbiCreationTarget,
)
from beehaiive.pbi_refinement_mutation import (
    PbiRefinementMutationProvider,
    PbiRefinementUpdateRequest,
    PbiRefinementUpdateResult,
)
from beehaiive.pbi_relations import (
    PbiRelationIssue,
    PbiRelationProvider,
    PbiRelationRequest,
    PbiRelationSnapshot,
)


class EnvironmentGitHubProvider:
    """Lazy provider used by the default FastAPI app."""

    def __init__(self) -> None:
        self._provider: ProjectProvider | None = None
        self._configuration: tuple[str | None, ...] | None = None
        self._lock = Lock()

    def _configured_provider(self) -> ProjectProvider:
        configuration = tuple(
            os.environ.get(name)
            for name in (
                "GITHUB_TOKEN",
                "GH_TOKEN",
                "GITHUB_PROJECT_OWNER",
                "GITHUB_PROJECT_NUMBER",
                "GITHUB_PROJECT_OWNER_TYPE",
                DISCOVERY_CACHE_SECONDS_ENV,
            )
        )
        with self._lock:
            if self._provider is None or configuration != self._configuration:
                self._provider = GitHubProjectProvider.from_environment()
                self._configuration = configuration
            return self._provider

    def discover_project(self, project_id: str) -> ProjectSnapshot:
        return self._configured_provider().discover_project(project_id)

    def invalidate_discovery_cache(self) -> None:
        provider = self._configured_provider()
        invalidate = getattr(provider, "invalidate_discovery_cache", None)
        if callable(invalidate):
            invalidate()

    def prepare_pbi_creation(self, request: PbiCreationRequest) -> PbiCreationTarget:
        provider = cast(PbiCreationProvider, self._configured_provider())
        return provider.prepare_pbi_creation(request)

    def create_pbi(
        self,
        request: PbiCreationRequest,
        target: PbiCreationTarget,
        progress: PbiCreationProgress,
        checkpoint: Callable[[PbiCreationProgress], None],
    ) -> PbiCreationResult:
        provider = cast(PbiCreationProvider, self._configured_provider())
        return provider.create_pbi(request, target, progress, checkpoint)

    def apply_pbi_refinement(
        self, request: PbiRefinementUpdateRequest
    ) -> PbiRefinementUpdateResult:
        provider = cast(PbiRefinementMutationProvider, self._configured_provider())
        return provider.apply_pbi_refinement(request)

    def prepare_pbi_relations(self, request: PbiRelationRequest) -> PbiRelationSnapshot:
        provider = cast(PbiRelationProvider, self._configured_provider())
        return provider.prepare_pbi_relations(request)

    def list_pbi_sub_issues(
        self, repository: str, parent_issue_number: int
    ) -> tuple[PbiRelationIssue, ...]:
        provider = cast(PbiRelationProvider, self._configured_provider())
        return provider.list_pbi_sub_issues(repository, parent_issue_number)

    def get_pbi_parent_issue_number(
        self, repository: str, child_issue_number: int
    ) -> int | None:
        provider = cast(PbiRelationProvider, self._configured_provider())
        return provider.get_pbi_parent_issue_number(repository, child_issue_number)

    def list_pbi_blocked_by(
        self, repository: str, issue_number: int
    ) -> tuple[PbiRelationIssue, ...]:
        provider = cast(PbiRelationProvider, self._configured_provider())
        return provider.list_pbi_blocked_by(repository, issue_number)

    def list_pbi_blocking(
        self, repository: str, issue_number: int
    ) -> tuple[PbiRelationIssue, ...]:
        provider = cast(PbiRelationProvider, self._configured_provider())
        return provider.list_pbi_blocking(repository, issue_number)

    def add_pbi_sub_issue(
        self, repository: str, parent_issue_number: int, child_issue_id: int
    ) -> None:
        provider = cast(PbiRelationProvider, self._configured_provider())
        provider.add_pbi_sub_issue(repository, parent_issue_number, child_issue_id)

    def add_pbi_dependency(
        self, repository: str, blocked_issue_number: int, blocker_issue_id: int
    ) -> None:
        provider = cast(PbiRelationProvider, self._configured_provider())
        provider.add_pbi_dependency(repository, blocked_issue_number, blocker_issue_id)

    def create_handoff(self, request: HandoffRequest) -> HandoffResult:
        return self._configured_provider().create_handoff(request)

    def resolve_base_branch(self, repository: str, requested: str | None) -> str:
        return self._configured_provider().resolve_base_branch(repository, requested)

    def validate_handoff(
        self, repository: str, branch: str, requested_base: str | None
    ) -> str:
        return self._configured_provider().validate_handoff(
            repository, branch, requested_base
        )

    def get_pull_request(self, repository: str, number: int) -> PullRequestSnapshot:
        return self._configured_provider().get_pull_request(repository, number)

    def complete_approved_handoff(
        self,
        request: HandoffRequest,
        pull_request_number: int,
        pull_request_url: str,
        expected_head: str,
        authorization: Mapping[str, object] | None,
    ) -> Mapping[str, object]:
        return self._configured_provider().complete_approved_handoff(
            request,
            pull_request_number,
            pull_request_url,
            expected_head,
            authorization,
        )

    def update_source_branch(
        self,
        snapshot: PullRequestSnapshot,
        worktree: str | Path,
        expected_head: str,
        repaired_head: str,
    ) -> None:
        self._configured_provider().update_source_branch(
            snapshot, worktree, expected_head, repaired_head
        )


__all__ = ["EnvironmentGitHubProvider"]
