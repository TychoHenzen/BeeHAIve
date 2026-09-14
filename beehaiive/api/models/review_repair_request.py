from ._common import BaseModel, Field

__all__ = ["ReviewRepairRequest"]


class ReviewRepairRequest(BaseModel):
    finding_ids: list[str] = Field(min_length=1, max_length=20)
