from ._common import BaseModel, Field

__all__ = ["ReviewHandoffRequest"]


class ReviewHandoffRequest(BaseModel):
    head_sha: str = Field(min_length=1, max_length=200)
