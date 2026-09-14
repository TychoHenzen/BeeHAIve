from __future__ import annotations

from enum import StrEnum


class ReviewCycleStatus(StrEnum):
    """Lifecycle state of one pull-request review cycle."""

    ACTIVE = "active"
    PASSED = "passed"
    FAILED = "failed"
    HUMAN_APPROVED = "human_approved"
    SUPERSEDED = "superseded"
