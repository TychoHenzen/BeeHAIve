from typing import Literal

from .dashboard_action_base import DashboardActionBase

__all__ = ["DashboardSynchronizeRequest"]


class DashboardSynchronizeRequest(DashboardActionBase):
    action: Literal["synchronize", "sync"]
