from typing import Literal

from .dashboard_action_base import DashboardActionBase

__all__ = ["DashboardCommitPushRequest"]


class DashboardCommitPushRequest(DashboardActionBase):
    action: Literal["commit_push"]
    repository: str
    pbi_number: int
    run_id: str
