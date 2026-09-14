from ._common import BaseModel, ConfigDict, Field

__all__ = ["PbiCreatedIssueReferenceBody"]


class PbiCreatedIssueReferenceBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(strict=True, min_length=1, max_length=200)
    number: int = Field(strict=True, gt=0, le=2_147_483_647)
    url: str = Field(strict=True, min_length=1, max_length=2_000)
