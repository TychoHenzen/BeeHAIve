from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .constants import (
    _EFFORT_SCALE_LABEL,
    _EPIC_SCALE_LABEL,
    _PRIORITY_SCALE_LABEL,
    MAX_REFINEMENT_BODY_LENGTH,
    MAX_REFINEMENT_LABELS,
    REFINEMENT_SECTION_ORDER,
)
from .pbi_refinement_mutation_error import PbiRefinementMutationError
from .text import _contains_managed_heading, is_pbi_refinement_scale_label

__all__ = ["PbiRefinementUpdateRequest"]


@dataclass(frozen=True, slots=True)
class PbiRefinementUpdateRequest:
    project_id: str
    repository: str
    pbi_number: int
    sections: Mapping[str, str]
    priority_label: str
    effort_label: str
    standard_labels: tuple[str, ...] = ()

    def validate(self) -> None:
        if not self.project_id or self.project_id != self.project_id.strip():
            raise PbiRefinementMutationError(
                "Project id is required", code="invalid_project"
            )
        if (
            not self.repository
            or self.repository != self.repository.strip()
            or self.repository.count("/") != 1
        ):
            raise PbiRefinementMutationError(
                "Repository must use owner/name format", code="invalid_repository"
            )
        if (
            type(self.pbi_number) is not int
            or not 1 <= self.pbi_number <= 2_147_483_647
        ):
            raise PbiRefinementMutationError(
                "PBI number is outside the supported range", code="invalid_pbi_number"
            )
        if set(self.sections) != set(REFINEMENT_SECTION_ORDER):
            raise PbiRefinementMutationError(
                "Exactly the five required refinement sections are required",
                code="invalid_sections",
            )
        total_section_length = 0
        for name in REFINEMENT_SECTION_ORDER:
            value = self.sections[name]
            if not value.strip():
                raise PbiRefinementMutationError(
                    f"{name} section is required", code="invalid_sections"
                )
            total_section_length += len(value)
            if _contains_managed_heading(value):
                raise PbiRefinementMutationError(
                    "Section content cannot contain a managed level-two heading",
                    code="invalid_sections",
                )
        if total_section_length > MAX_REFINEMENT_BODY_LENGTH:
            raise PbiRefinementMutationError(
                "Refinement sections exceed the body limit", code="body_too_large"
            )
        if not _PRIORITY_SCALE_LABEL.fullmatch(self.priority_label):
            raise PbiRefinementMutationError(
                "A live priority label is required", code="invalid_priority_label"
            )
        if not (
            _EFFORT_SCALE_LABEL.fullmatch(self.effort_label)
            or self.effort_label == _EPIC_SCALE_LABEL
        ):
            raise PbiRefinementMutationError(
                "A live effort label is required", code="invalid_effort_label"
            )
        labels = (
            self.priority_label,
            self.effort_label,
            *self.standard_labels,
        )
        if len(self.standard_labels) > MAX_REFINEMENT_LABELS:
            raise PbiRefinementMutationError(
                "At most 20 standard labels may be supplied", code="invalid_labels"
            )
        normalized: set[str] = set()
        for label in labels:
            if not label or label != label.strip() or len(label) > 100:
                raise PbiRefinementMutationError(
                    "Labels must contain 1 to 100 non-whitespace characters",
                    code="invalid_labels",
                )
            key = label.casefold()
            if key in normalized:
                raise PbiRefinementMutationError(
                    "Requested labels must be unique", code="invalid_labels"
                )
            normalized.add(key)
        if any(is_pbi_refinement_scale_label(label) for label in self.standard_labels):
            raise PbiRefinementMutationError(
                "Standard labels cannot replace priority or effort labels",
                code="invalid_labels",
            )
