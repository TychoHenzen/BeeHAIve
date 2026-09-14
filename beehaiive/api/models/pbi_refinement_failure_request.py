from ._common import BaseModel, Field

__all__ = ["PbiRefinementFailureRequest"]


class PbiRefinementFailureRequest(BaseModel):
    expected_revision: int = Field(ge=0)
    reason: str = Field(min_length=1, max_length=500)
    retryable: bool
