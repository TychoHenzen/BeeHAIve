from ._common import BaseModel, Field

__all__ = ["ConflictRepairRequest"]


class ConflictRepairRequest(BaseModel):
    repository: str = Field(min_length=1, max_length=300)
    pull_request_number: int = Field(gt=0)
