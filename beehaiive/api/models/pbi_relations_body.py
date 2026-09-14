from ._common import (
    MAX_PBI_RELATION_CHILDREN,
    MAX_PBI_RELATION_DEPENDENCIES,
    BaseModel,
    ConfigDict,
    Field,
    cast,
)
from .pbi_created_issue_result_body import PbiCreatedIssueResultBody
from .pbi_relation_dependency_body import PbiRelationDependencyBody

__all__ = ["PbiRelationsBody"]


class PbiRelationsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    children: list[PbiCreatedIssueResultBody] = Field(
        min_length=1, max_length=MAX_PBI_RELATION_CHILDREN
    )
    dependencies: list[PbiRelationDependencyBody] = Field(
        default_factory=lambda: cast(list[PbiRelationDependencyBody], []),
        max_length=MAX_PBI_RELATION_DEPENDENCIES,
    )
