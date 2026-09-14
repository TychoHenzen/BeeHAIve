from ._common import BaseModel, ConfigDict, Field

__all__ = ["PbiCreatedProjectReferenceBody"]


class PbiCreatedProjectReferenceBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str = Field(strict=True, min_length=1, max_length=200)
    status: str = Field(strict=True, min_length=1, max_length=100)
