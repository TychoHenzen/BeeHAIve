from ._common import BaseModel, Field, WorkflowRole

__all__ = ["WorkflowHandoffRequest"]


class WorkflowHandoffRequest(BaseModel):
    lease_id: str = Field(min_length=1, max_length=100)
    source_role: WorkflowRole
    target_role: WorkflowRole
    commit_sha: str = Field(default="", max_length=200)
    source_state: str = Field(default="", max_length=1_000)
    approval_required: bool = False
