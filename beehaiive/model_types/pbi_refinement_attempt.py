from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .pbi_refinement_question import PbiRefinementQuestion
from .refinement_status import RefinementStatus

__all__ = ["PbiRefinementAttempt"]


@dataclass(frozen=True, slots=True)
class PbiRefinementAttempt:
    """Restart-safe refinement state for one Project, repository, and PBI."""

    attempt_id: str
    project_id: str
    repository: str
    pbi_number: int
    generation: int
    reopen_count: int
    revision: int
    status: RefinementStatus
    questions: tuple[PbiRefinementQuestion, ...]
    decision: Mapping[str, object] | None
    failure_reason: str | None
    retryable_failure: bool
    history: tuple[Mapping[str, object], ...]
    authorization: Mapping[str, str]
    created_at: str
    updated_at: str

    def as_dict(self) -> dict[str, object]:
        return {
            "attempt_id": self.attempt_id,
            "project_id": self.project_id,
            "repository": self.repository,
            "pbi_number": self.pbi_number,
            "generation": self.generation,
            "reopen_count": self.reopen_count,
            "revision": self.revision,
            "status": self.status.value,
            "questions": [question.as_dict() for question in self.questions],
            "decision": dict(self.decision) if self.decision is not None else None,
            "failure_reason": self.failure_reason,
            "retryable_failure": self.retryable_failure,
            "history": [dict(item) for item in self.history],
            "authorization": dict(self.authorization),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
