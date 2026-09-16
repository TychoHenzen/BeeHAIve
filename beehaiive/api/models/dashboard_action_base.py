from ._common import BaseModel, ConfigDict, Field

__all__ = ["DashboardActionBase"]


class DashboardActionBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approved: bool = Field(default=False, strict=True)
