from typing import Literal

from ._common import Field
from .dashboard_action_base import DashboardActionBase

__all__ = ["DashboardAnswerQuestionRequest"]


class DashboardAnswerQuestionRequest(DashboardActionBase):
    action: Literal["answer_question"]
    repository: str
    pbi_number: int
    run_id: str
    question_id: str = Field(min_length=1, max_length=64)
    revision: int = Field(ge=1)
    answer: str = Field(min_length=1, max_length=1_000)
