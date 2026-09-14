from ._common import BaseModel, Field

__all__ = ["PbiRefinementApplyRequest"]


class PbiRefinementApplyRequest(BaseModel):
    sections: dict[str, str] = Field(min_length=5, max_length=5)
    priority_label: str = Field(min_length=1, max_length=100)
    effort_label: str = Field(min_length=1, max_length=100)
    standard_labels: list[str] = Field(default_factory=list, max_length=20)
