from typing import Literal

from pydantic import field_validator

from ._common import Field
from .dashboard_action_base import DashboardActionBase

__all__ = ["DashboardRetryRequest"]


class DashboardRetryRequest(DashboardActionBase):
    action: Literal["retry"]
    repository: str
    pbi_number: int = Field(strict=True, gt=0, le=2_147_483_647)
    run_id: str
    worker_id: str | None = Field(default=None, max_length=200)

    @field_validator("worker_id")
    @classmethod
    def reject_blank_worker_id(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("A worker ID is required")
        return value
