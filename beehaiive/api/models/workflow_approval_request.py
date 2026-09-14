from ._common import BaseModel, Field

__all__ = ["WorkflowApprovalRequest"]


class WorkflowApprovalRequest(BaseModel):
    note: str = Field(default="", max_length=1_000)
