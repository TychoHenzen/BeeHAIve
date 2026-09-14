from typing import Literal

from ._common import BaseModel

__all__ = ["MetaReviewDecisionRequest"]


class MetaReviewDecisionRequest(BaseModel):
    decision: Literal["accept", "reject"]
