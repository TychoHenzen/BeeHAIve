from ._common import BaseModel, Field

__all__ = ["ReviewApprovalRequest"]


class ReviewApprovalRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=1_000)
