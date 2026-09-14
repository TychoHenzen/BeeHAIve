from __future__ import annotations

from enum import StrEnum


class RepairStatus(StrEnum):
    """Durable lifecycle state for one pull-request conflict repair."""

    RUNNING = "running"
    BLOCKED = "blocked"
    AWAITING_APPROVAL = "awaiting_approval"
    AWAITING_CLARIFICATION = "awaiting_clarification"
    NOT_REQUIRED = "not_required"
    SUCCEEDED = "succeeded"
