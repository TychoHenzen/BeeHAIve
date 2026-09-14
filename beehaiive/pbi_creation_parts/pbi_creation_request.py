from __future__ import annotations

from dataclasses import dataclass

from .pbi_creation_validation_error import PbiCreationValidationError

__all__ = ["PbiCreationRequest"]


@dataclass(frozen=True, slots=True)
class PbiCreationRequest:
    project_id: str
    repository: str
    title: str
    body: str
    labels: tuple[str, ...] = ()

    def validate(self) -> None:
        if not self.project_id or self.project_id != self.project_id.strip():
            raise PbiCreationValidationError("Project id is required")
        if (
            not self.repository
            or self.repository != self.repository.strip()
            or self.repository.count("/") != 1
        ):
            raise PbiCreationValidationError(
                "Repository must use owner/name format", code="invalid_repository"
            )
        if not self.title.strip() or len(self.title) > 256:
            raise PbiCreationValidationError(
                "Title must contain 1 to 256 characters", code="invalid_title"
            )
        if not self.body.strip() or len(self.body) > 65_536:
            raise PbiCreationValidationError(
                "Body must contain 1 to 65536 characters", code="invalid_body"
            )
        if len(self.labels) > 20:
            raise PbiCreationValidationError(
                "At most 20 labels may be supplied", code="invalid_labels"
            )
        if any(
            not label or label != label.strip() or len(label) > 100
            for label in self.labels
        ):
            raise PbiCreationValidationError(
                "Labels must contain 1 to 100 non-whitespace characters",
                code="invalid_labels",
            )
        if len(set(self.labels)) != len(self.labels):
            raise PbiCreationValidationError(
                "Labels must not contain duplicates", code="invalid_labels"
            )
