from ._common import BaseModel, Field

__all__ = ["WorkflowStopRequest"]


class WorkflowStopRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=400)
