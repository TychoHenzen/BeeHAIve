from ._common import BaseModel, Field

__all__ = ["WorkflowModelCallRequest"]


class WorkflowModelCallRequest(BaseModel):
    lease_id: str = Field(min_length=1, max_length=100)
