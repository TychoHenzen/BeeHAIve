from typing import Literal

from ._common import BaseModel, ConfigDict, Field
from .pbi_created_issue_reference_body import PbiCreatedIssueReferenceBody
from .pbi_created_project_reference_body import PbiCreatedProjectReferenceBody

__all__ = ["PbiCreatedIssueResultBody"]


class PbiCreatedIssueResultBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["complete"]
    repository: str = Field(strict=True, min_length=3, max_length=300)
    issue: PbiCreatedIssueReferenceBody
    project: PbiCreatedProjectReferenceBody
    labels: list[str] | None = Field(default=None, max_length=20)
    completed_steps: list[str] | None = Field(default=None, max_length=20)
