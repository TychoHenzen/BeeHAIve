from ._common import BaseModel, Field

__all__ = ["ReviewStartRequest"]


class ReviewStartRequest(BaseModel):
    pull_request_id: str = Field(min_length=1, max_length=200)
    head_sha: str = Field(min_length=1, max_length=200)
