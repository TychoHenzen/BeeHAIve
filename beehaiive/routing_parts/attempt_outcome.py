from __future__ import annotations

from enum import StrEnum


class AttemptOutcome(StrEnum):
    """Result reported for one model attempt."""

    FAILURE = "failure"
    RETRY = "retry"
    SUCCESS = "success"
