from typing import Literal

from ._common import ConfigDict, Field
from .dashboard_action_base import DashboardActionBase


class DashboardGraphSafetyRequest(DashboardActionBase):
    model_config = ConfigDict(extra="forbid")

    action: Literal["graph_evaluate", "graph_review", "graph_activate"]
    workflow_id: str = Field(min_length=1, max_length=128)
    candidate: dict[str, object]
    fixtures: dict[str, dict[str, object]] = Field(default_factory=dict)
    baseline: dict[str, object] | None = None
    baseline_fixtures: dict[str, dict[str, object]] = Field(default_factory=dict)


class DashboardGraphRollbackRequest(DashboardActionBase):
    model_config = ConfigDict(extra="forbid")

    action: Literal["graph_rollback"]
    workflow_id: str = Field(min_length=1, max_length=128)
    revision: int = Field(strict=True, gt=0, le=2_147_483_647)


__all__ = ["DashboardGraphRollbackRequest", "DashboardGraphSafetyRequest"]
