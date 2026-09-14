from __future__ import annotations

from enum import StrEnum

__all__ = ["RunStatus"]


class RunStatus(StrEnum):
    """Durable status of a repository writer run."""

    ACTIVE = "active"
    AWAITING_OPERATOR = "awaiting_operator"
    FAILED = "failed"
    COMPLETED = "completed"
