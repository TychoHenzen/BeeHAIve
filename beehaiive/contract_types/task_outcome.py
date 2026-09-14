from __future__ import annotations

from enum import StrEnum

__all__ = ["TaskOutcome"]


class TaskOutcome(StrEnum):
    """Terminal outcome of one executable worker step."""

    PASS = "pass"
    FAIL = "fail"
    BLOCKED = "blocked"
    QUESTION = "question"
