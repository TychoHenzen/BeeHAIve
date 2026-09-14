from __future__ import annotations

from enum import StrEnum


class ReviewRepairStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    PUSHING = "pushing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    HUMAN_ACTION_REQUIRED = "human_action_required"
