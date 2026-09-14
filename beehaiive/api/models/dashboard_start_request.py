from typing import Literal

from .dashboard_action_base import DashboardActionBase

__all__ = ["DashboardStartRequest"]


class DashboardStartRequest(DashboardActionBase):
    action: Literal["start"]
    repository: str | None = None
    worker_id: str | None = None
