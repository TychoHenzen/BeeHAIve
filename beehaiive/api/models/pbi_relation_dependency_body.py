from ._common import BaseModel, ConfigDict, Field

__all__ = ["PbiRelationDependencyBody"]


class PbiRelationDependencyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    blocked_issue_number: int = Field(strict=True, gt=0, le=2_147_483_647)
    blocked_by_issue_number: int = Field(strict=True, gt=0, le=2_147_483_647)
