from __future__ import annotations

from enum import StrEnum


class HandoffStatus(StrEnum):
    """State exposed to the next role or to an operator."""

    BLOCKED = "blocked"
    AWAITING_APPROVAL = "awaiting_approval"
    AWAITING_CLARIFICATION = "awaiting_clarification"
    ACCEPTED = "accepted"
    STOPPED = "stopped"
