from ._common import BaseModel, Field, ReaderStatus, ReviewConcern

__all__ = ["ReviewReaderRequest"]


class ReviewReaderRequest(BaseModel):
    concern: ReviewConcern
    status: ReaderStatus
    findings: list[str] = Field(default_factory=list, max_length=20)
    evidence_refs: list[str] = Field(default_factory=list, max_length=100)
    reader: str = Field(default="automated", min_length=1, max_length=100)
