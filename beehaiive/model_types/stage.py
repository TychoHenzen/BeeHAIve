from __future__ import annotations

from enum import StrEnum

__all__ = ["Stage"]


class Stage(StrEnum):
    """Stages owned by the project orchestrator."""

    BACKLOG = "backlog"
    REFINE = "refine"
    IMPLEMENT = "implement"
    PULL_REQUEST = "pull_request"
