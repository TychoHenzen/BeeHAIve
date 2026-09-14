from ._common import BaseModel, Field

__all__ = ["WorkflowClarificationAnswer"]


class WorkflowClarificationAnswer(BaseModel):
    answer: str = Field(min_length=1, max_length=1_000)
