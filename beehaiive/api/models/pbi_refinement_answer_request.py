from ._common import Annotated, BaseModel, Field

__all__ = ["PbiRefinementAnswerRequest"]


class PbiRefinementAnswerRequest(BaseModel):
    answer: str = Field(min_length=1, max_length=1_000)
    expected_revision: int = Field(ge=0)
    evidence_refs: list[Annotated[str, Field(min_length=1, max_length=512)]] = Field(
        default_factory=list, max_length=10
    )
