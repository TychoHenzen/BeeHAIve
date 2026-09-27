from ._common import BaseModel, ConfigDict, Field


class StationIssueActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str = Field(default="", max_length=500)


__all__ = ["StationIssueActionRequest"]
