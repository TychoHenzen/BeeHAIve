"""Domain models shared by providers, storage, and the HTTP service."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum


class Stage(StrEnum):
    """Stages owned by the project orchestrator."""

    BACKLOG = "backlog"
    REFINE = "refine"
    IMPLEMENT = "implement"
    PULL_REQUEST = "pull_request"


def project_stage_from_status(status: str | None) -> Stage | None:
    normalized = (status or "").strip().lower()
    return {
        "backlog": Stage.BACKLOG,
        "todo": Stage.REFINE,
        "in progress": Stage.IMPLEMENT,
    }.get(normalized)


class RunStatus(StrEnum):
    """Durable status of a repository writer run."""

    ACTIVE = "active"
    FAILED = "failed"
    COMPLETED = "completed"


PROJECT_TERMINAL_STATUSES = frozenset({"done", "completed", "closed", "merged"})
ARCHIVE_PROJECT_STATUS = "done"


def _empty_metadata() -> dict[str, object]:
    return {}


@dataclass(frozen=True, slots=True)
class PbiSnapshot:
    """A PBI discovered from a project provider."""

    repository: str
    number: int
    title: str
    stage: Stage | None = Stage.BACKLOG
    planning_status: str | None = None
    claimable: bool = True
    metadata: Mapping[str, object] = field(default_factory=_empty_metadata)


@dataclass(frozen=True, slots=True)
class RepositorySnapshot:
    """A linked repository and its discovered PBIs."""

    name: str
    pbis: tuple[PbiSnapshot, ...] = ()


@dataclass(frozen=True, slots=True)
class ProjectSnapshot:
    """A selected project with all linked repositories."""

    project_id: str
    name: str
    repositories: tuple[RepositorySnapshot, ...]


@dataclass(frozen=True, slots=True)
class HandoffRequest:
    """Information required to create a branch and pull request."""

    project_id: str
    repository: str
    pbi_number: int
    title: str
    branch: str
    base_branch: str | None
    body: str
    run_id: str


@dataclass(frozen=True, slots=True)
class HandoffIntent:
    """Request details persisted before an external handoff begins."""

    run: RunState
    branch: str
    base_branch: str | None
    body: str


@dataclass(frozen=True, slots=True)
class HandoffResult:
    """The provider's durable branch and pull-request identifiers."""

    branch: str
    pull_request_url: str
    pull_request_number: int | None = None


@dataclass(frozen=True, slots=True)
class PullRequestSnapshot:
    """Current pull-request identity and mergeability evidence."""

    repository: str
    number: int
    pull_request_id: str
    url: str
    state: str
    merged: bool
    source_branch: str | None
    source_head: str | None
    target_branch: str | None
    target_head: str | None
    mergeable: str | None
    merge_state: str | None
    evidence_error: str | None = None

    @property
    def conflict_state(self) -> str:
        """Return conflicting, clean, or unknown without guessing."""

        if self.evidence_error is not None:
            return "unknown"
        if (
            self.state != "OPEN"
            or self.merged
            or not self.source_branch
            or not self.source_head
            or not self.target_branch
            or not self.target_head
        ):
            return "unknown"
        if self.mergeable == "CONFLICTING" and self.merge_state == "DIRTY":
            return "conflicting"
        if self.mergeable == "MERGEABLE" and self.merge_state in {
            "BEHIND",
            "BLOCKED",
            "CLEAN",
            "HAS_HOOKS",
            "UNSTABLE",
        }:
            return "clean"
        return "unknown"

    def as_dict(self) -> dict[str, object]:
        return {
            "repository": self.repository,
            "number": self.number,
            "pull_request_id": self.pull_request_id,
            "url": self.url,
            "state": self.state,
            "merged": self.merged,
            "source_branch": self.source_branch,
            "source_head": self.source_head,
            "target_branch": self.target_branch,
            "target_head": self.target_head,
            "mergeable": self.mergeable,
            "merge_state": self.merge_state,
            "conflict_state": self.conflict_state,
            "evidence_error": self.evidence_error,
        }


@dataclass(frozen=True, slots=True)
class RunState:
    """A PBI run, including the stage needed for restart recovery."""

    run_id: str
    project_id: str
    repository: str
    pbi_number: int
    title: str
    stage: Stage
    status: RunStatus
    attempt: int
    branch: str | None = None
    pull_request_url: str | None = None
    last_error: str | None = None
    owner_id: str | None = None
    lease_token: str | None = None
    lease_expires_at: str | None = None
    last_result: str | None = None


@dataclass(frozen=True, slots=True)
class RoutingFailure:
    """A durable routing failure waiting for delivery to the routing store."""

    transition_id: str
    run_id: str
    error: str
    input_tokens: int
    output_tokens: int
    recursive_spawn_depth: int
