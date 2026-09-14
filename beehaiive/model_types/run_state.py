from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .run_status import RunStatus
from .stage import Stage

__all__ = ["RunState"]


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
    task_contract: Mapping[str, object] | None = None
    task_result: Mapping[str, object] | None = None
    task_answer: str | None = None
