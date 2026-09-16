from typing import Literal

from pydantic import field_validator

from ._common import Field
from .dashboard_action_base import DashboardActionBase

__all__ = ["DashboardStartRequest"]


class DashboardStartRequest(DashboardActionBase):
    action: Literal["start", "claim"]
    repository: str | None = None
    pbi_number: int | None = Field(default=None, strict=True, gt=0)
    worker_id: str | None = Field(default=None, max_length=200)

    @field_validator("worker_id")
    @classmethod
    def reject_blank_worker_id(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("A worker ID is required")
        return value
