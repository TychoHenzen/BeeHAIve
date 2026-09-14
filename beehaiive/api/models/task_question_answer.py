from ._common import BaseModel, Field

__all__ = ["TaskQuestionAnswer"]


class TaskQuestionAnswer(BaseModel):
    question_id: str = Field(min_length=1, max_length=64)
    revision: int = Field(ge=1)
    answer: str = Field(min_length=1, max_length=1_000)
