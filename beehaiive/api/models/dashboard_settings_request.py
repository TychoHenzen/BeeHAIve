from ._common import BaseModel, ConfigDict, Field

__all__ = ["DashboardSettingsRequest"]


class DashboardSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approved: bool = Field(default=False, strict=True)
    projects: list[str] | None = Field(default=None, min_length=1, max_length=32)
    workflow_id: str | None = Field(default=None, max_length=128)
