from __future__ import annotations

from dataclasses import dataclass

from .constants import MAX_PBI_RELATION_CHILDREN, MAX_PBI_RELATION_DEPENDENCIES
from .pbi_created_issue_reference import PbiCreatedIssueReference
from .pbi_relation_dependency import PbiRelationDependency
from .pbi_relation_validation_error import PbiRelationValidationError

__all__ = ["PbiRelationRequest"]


@dataclass(frozen=True, slots=True)
class PbiRelationRequest:
    project_id: str
    repository: str
    parent_issue_number: int
    children: tuple[PbiCreatedIssueReference, ...]
    dependencies: tuple[PbiRelationDependency, ...]

    def validate(self) -> None:
        if not self.project_id.strip():
            raise PbiRelationValidationError(
                "Project id is required", code="invalid_project"
            )
        owner, separator, name = self.repository.partition("/")
        if not separator or not owner or not name or "/" in name:
            raise PbiRelationValidationError(
                "Repository must use owner/name format", code="invalid_repository"
            )
        if self.parent_issue_number <= 0:
            raise PbiRelationValidationError(
                "Parent issue number must be positive", code="invalid_parent"
            )
        if not 1 <= len(self.children) <= MAX_PBI_RELATION_CHILDREN:
            raise PbiRelationValidationError(
                "Child count is outside the supported range", code="invalid_children"
            )
        child_numbers: set[int] = set()
        child_node_ids: set[str] = set()
        project_item_ids: set[str] = set()
        for child in self.children:
            if (
                child.number <= 0
                or not child.node_id.strip()
                or not child.url.strip()
                or not child.project_item_id.strip()
            ):
                raise PbiRelationValidationError(
                    "Created child reference is incomplete", code="invalid_child"
                )
            if child.repository.casefold() != self.repository.casefold():
                raise PbiRelationValidationError(
                    "Child repository does not match the request repository",
                    code="cross_repository_child",
                )
            if child.number == self.parent_issue_number:
                raise PbiRelationValidationError(
                    "Parent cannot be one of its own children", code="self_link"
                )
            if (
                child.number in child_numbers
                or child.node_id in child_node_ids
                or child.project_item_id in project_item_ids
            ):
                raise PbiRelationValidationError(
                    "Child references must be unique", code="duplicate_child"
                )
            child_numbers.add(child.number)
            child_node_ids.add(child.node_id)
            project_item_ids.add(child.project_item_id)
        if len(self.dependencies) > MAX_PBI_RELATION_DEPENDENCIES:
            raise PbiRelationValidationError(
                "Dependency count exceeds the supported limit",
                code="too_many_dependencies",
            )
        dependency_pairs: set[tuple[int, int]] = set()
        for dependency in self.dependencies:
            blocked = dependency.blocked_issue_number
            blocker = dependency.blocked_by_issue_number
            if blocked <= 0 or blocker <= 0:
                raise PbiRelationValidationError(
                    "Dependency issue numbers must be positive",
                    code="invalid_dependency",
                )
            if blocked == blocker:
                raise PbiRelationValidationError(
                    "An issue cannot block itself", code="self_dependency"
                )
            if blocked not in child_numbers or blocker not in child_numbers:
                raise PbiRelationValidationError(
                    "Dependencies must refer to declared children",
                    code="dependency_outside_children",
                )
            pair = (blocked, blocker)
            if pair in dependency_pairs:
                raise PbiRelationValidationError(
                    "Dependency declarations must be unique",
                    code="duplicate_dependency",
                )
            dependency_pairs.add(pair)
