from __future__ import annotations

from enum import StrEnum

__all__ = ["RefinementStatus"]


class RefinementStatus(StrEnum):
    """Durable state of one PBI refinement attempt."""

    AWAITING_ANSWERS = "awaiting_answers"
    EVALUATING = "evaluating"
    COMPLETED = "completed"
    FAILED = "failed"
