from typing import Literal

from ._common import Field, Stage
from .dashboard_action_base import DashboardActionBase

__all__ = ["DashboardAdvanceRequest"]


class DashboardAdvanceRequest(DashboardActionBase):
    action: Literal["advance"]
    repository: str
    pbi_number: int = Field(strict=True, gt=0, le=2_147_483_647)
    run_id: str
    target: Stage
