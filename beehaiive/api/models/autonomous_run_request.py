from ._common import BaseModel, ConfigDict, Field

__all__ = ["AutonomousRunRequest"]


class AutonomousRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approved: bool = Field(default=False, strict=True)
    repository: str | None = None
    pbi_number: int | None = Field(default=None, strict=True, gt=0)
    workflow_id: str | None = Field(default=None, max_length=128)
