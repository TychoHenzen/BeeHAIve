from typing import Literal

from .dashboard_action_base import DashboardActionBase

__all__ = ["DashboardStopRequest"]


class DashboardStopRequest(DashboardActionBase):
    action: Literal["stop"]
    run_id: str
    repository: str | None = None
    reason: str = ""
