from typing import Literal

from pydantic import field_validator

from ._common import Field
from .dashboard_action_base import DashboardActionBase

__all__ = ["DashboardStopRequest"]


class DashboardStopRequest(DashboardActionBase):
    action: Literal["stop"]
    run_id: str
    repository: str | None = None
    reason: str = Field(default="", max_length=500)

    @field_validator("reason")
    @classmethod
    def reject_whitespace_reason(cls, value: str) -> str:
        if value and not value.strip():
            raise ValueError("A stop reason is required")
        return value
