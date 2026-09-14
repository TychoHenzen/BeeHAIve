from ._common import BaseModel, Field

__all__ = ["ReviewResolutionRequest"]


class ReviewResolutionRequest(BaseModel):
    resolution: str = Field(min_length=1, max_length=1_000)
