from typing import Literal

from .dashboard_action_base import DashboardActionBase

__all__ = ["DashboardApproveRequest"]


class DashboardApproveRequest(DashboardActionBase):
    action: Literal["approve"]
    repository: str
    pbi_number: int
    run_id: str
