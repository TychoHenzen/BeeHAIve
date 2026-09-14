from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

from .constants import MAX_CONTRACT_TEXT
from .contract_error import ContractError
from .validation import _redact_text, _text

__all__ = ["ArtifactRequirement"]


@dataclass(frozen=True, slots=True)
class ArtifactRequirement:
    """One artifact that a task may require from the worker."""

    artifact_id: str
    description: str = ""
    required: bool = True

    def __post_init__(self) -> None:
        if not self.artifact_id.strip():
            raise ContractError("Artifact id is required")
        if len(self.artifact_id) > MAX_CONTRACT_TEXT:
            raise ContractError("Artifact id is too long")
        if len(self.description) > MAX_CONTRACT_TEXT:
            raise ContractError("Artifact description is too long")

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.artifact_id,
            "description": _redact_text(self.description, MAX_CONTRACT_TEXT),
            "required": self.required,
        }

    @classmethod
    def from_value(cls, value: object) -> ArtifactRequirement:
        if not isinstance(value, Mapping):
            raise ContractError("Artifact requirements must be objects")
        mapping = cast(Mapping[str, object], value)
        artifact_id = _text(mapping.get("id"), "Artifact id")
        description = _text(
            mapping.get("description", ""), "Artifact description", required=False
        )
        required = mapping.get("required", True)
        if not isinstance(required, bool):
            raise ContractError("Artifact required must be boolean")
        return cls(artifact_id or "", description or "", required)
