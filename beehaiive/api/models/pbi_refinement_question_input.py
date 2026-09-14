from ._common import Annotated, BaseModel, Field

__all__ = ["PbiRefinementQuestionInput"]


class PbiRefinementQuestionInput(BaseModel):
    text: str = Field(min_length=1, max_length=1_000)
    evidence_refs: list[Annotated[str, Field(min_length=1, max_length=512)]] = Field(
        default_factory=list, max_length=10
    )
