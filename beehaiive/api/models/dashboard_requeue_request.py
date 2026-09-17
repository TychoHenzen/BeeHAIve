from typing import Literal

from ._common import Field
from .dashboard_action_base import DashboardActionBase

__all__ = ["DashboardRequeueRequest"]


class DashboardRequeueRequest(DashboardActionBase):
    action: Literal["requeue"]
    repository: str
    pbi_number: int = Field(strict=True, gt=0, le=2_147_483_647)
    run_id: str
