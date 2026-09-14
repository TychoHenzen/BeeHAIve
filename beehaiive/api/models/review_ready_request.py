from ._common import BaseModel, Field

__all__ = ["ReviewReadyRequest"]


class ReviewReadyRequest(BaseModel):
    pull_request_id: str = Field(min_length=1, max_length=200)
