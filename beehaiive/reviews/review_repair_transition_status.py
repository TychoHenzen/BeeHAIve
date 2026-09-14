from __future__ import annotations

from enum import StrEnum


class ReviewRepairTransitionStatus(StrEnum):
    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    COMPLETED = "completed"
    RETRY_REQUIRED = "retry_required"
    HUMAN_ACTION_REQUIRED = "human_action_required"
