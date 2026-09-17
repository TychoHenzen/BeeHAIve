from ._common import BaseModel, ConfigDict, Field

__all__ = ["SchedulerConfigRequest"]


class SchedulerConfigRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approved: bool = Field(default=False, strict=True)
    enabled: bool = Field(strict=True)
    poll_interval_seconds: float = Field(gt=0, allow_inf_nan=False)
    max_concurrency: int = Field(strict=True, gt=0)
