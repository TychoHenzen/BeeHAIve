from ._common import BaseModel, Field

__all__ = ["WorkflowWorkspaceRequest"]


class WorkflowWorkspaceRequest(BaseModel):
    agent_id: str = Field(min_length=1, max_length=400)
    branch: str = Field(min_length=1, max_length=400)
    worktree: str = Field(min_length=1, max_length=1_000)
    base_ref: str = Field(default="HEAD", min_length=1, max_length=400)
