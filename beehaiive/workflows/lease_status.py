from __future__ import annotations

from enum import StrEnum


class LeaseStatus(StrEnum):
    """Lifecycle state of one isolated worktree lease."""

    ACTIVE = "active"
    RETAINED = "retained"
    RELEASED = "released"
    STOPPED = "stopped"
