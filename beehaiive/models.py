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


class RunStatus(StrEnum):
    """Durable status of a repository writer run."""

    ACTIVE = "active"
    FAILED = "failed"
    COMPLETED = "completed"


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


@dataclass(frozen=True, slots=True)
class RoutingFailure:
    """A durable routing failure waiting for delivery to the routing store."""

    transition_id: str
    run_id: str
    error: str
    input_tokens: int
    output_tokens: int
    recursive_spawn_depth: int
