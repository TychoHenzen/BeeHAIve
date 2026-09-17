from typing import Literal

from ._common import Field
from .dashboard_action_base import DashboardActionBase

__all__ = ["DashboardIdeaCaptureRequest"]


class DashboardIdeaCaptureRequest(DashboardActionBase):
    action: Literal["capture_idea"]
    idea: str = Field(min_length=1, max_length=8_000)
