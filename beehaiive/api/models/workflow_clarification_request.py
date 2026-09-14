from ._common import BaseModel, Field

__all__ = ["WorkflowClarificationRequest"]


class WorkflowClarificationRequest(BaseModel):
    question: str = Field(min_length=1, max_length=1_000)
