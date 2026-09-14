from ._common import BaseModel, Field, ReviewConcern

__all__ = ["ReviewFindingRequest"]


class ReviewFindingRequest(BaseModel):
    concern: ReviewConcern
    summary: str = Field(min_length=1, max_length=1_000)
    evidence_refs: list[str] = Field(default_factory=list, max_length=100)
    file_path: str | None = Field(default=None, max_length=500)
    start_line: int | None = Field(default=None, gt=0, le=2_147_483_647)
    end_line: int | None = Field(default=None, gt=0, le=2_147_483_647)
    duplicate_target: str | None = Field(default=None, max_length=200)
