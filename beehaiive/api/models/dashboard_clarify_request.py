from typing import Literal

from .dashboard_action_base import DashboardActionBase

__all__ = ["DashboardClarifyRequest"]


class DashboardClarifyRequest(DashboardActionBase):
    action: Literal["clarify"]
    repository: str
    pbi_number: int
    run_id: str
    clarification: str = ""
