from ._common import BaseModel, ConfigDict, Field


class GraphSafetyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate: dict[str, object]
    baseline: dict[str, object] | None = None
    fixtures: dict[str, dict[str, object]] = Field(default_factory=dict)
    baseline_fixtures: dict[str, dict[str, object]] = Field(default_factory=dict)


class GraphRollbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_id: str = Field(min_length=1, max_length=128)
    revision: int = Field(gt=0, le=2_147_483_647)


__all__ = ["GraphRollbackRequest", "GraphSafetyRequest"]
