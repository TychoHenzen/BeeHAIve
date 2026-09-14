from ._common import BaseModel, Field
from .pbi_refinement_question_input import PbiRefinementQuestionInput

__all__ = ["PbiRefinementReopenRequest"]


class PbiRefinementReopenRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)
    questions: list[PbiRefinementQuestionInput] = Field(min_length=1, max_length=25)
