from ._common import BaseModel, Field

__all__ = ["TaskQuestionAnswer"]


class TaskQuestionAnswer(BaseModel):
    answer: str = Field(min_length=1, max_length=1_000)
