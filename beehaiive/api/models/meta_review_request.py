from ._common import (
    MAX_META_REVIEW_INPUT_TOKENS,
    MAX_META_REVIEW_RECORDS,
    BaseModel,
    Field,
)

__all__ = ["MetaReviewRequest"]


class MetaReviewRequest(BaseModel):
    since: str | None = Field(default=None, max_length=40)
    record_limit: int = Field(
        default=MAX_META_REVIEW_RECORDS, ge=1, le=MAX_META_REVIEW_RECORDS
    )
    input_token_limit: int = Field(
        default=MAX_META_REVIEW_INPUT_TOKENS,
        ge=1,
        le=MAX_META_REVIEW_INPUT_TOKENS,
    )
