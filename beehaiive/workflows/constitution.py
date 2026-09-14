from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, cast

from .helpers import enum_role, require_text
from .workflow_error import WorkflowError

if TYPE_CHECKING:
    from .workflow_role import WorkflowRole


@dataclass(frozen=True, slots=True)
class Constitution:
    """Machine-readable shared rules selected by workflow role."""

    version: int
    sections: Mapping[str, tuple[str, ...]]
    roles: Mapping[WorkflowRole, tuple[str, ...]]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "sections",
            MappingProxyType(
                {name: tuple(rules) for name, rules in self.sections.items()}
            ),
        )
        object.__setattr__(
            self,
            "roles",
            MappingProxyType(
                {role: tuple(sections) for role, sections in self.roles.items()}
            ),
        )

    @classmethod
    def load(cls, path: str | Path) -> Constitution:
        constitution_path = Path(path)
        try:
            decoded = json.loads(constitution_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkflowError(
                f"Cannot load constitution: {constitution_path}"
            ) from exc
        if not isinstance(decoded, dict):
            raise WorkflowError("Constitution must be a JSON object")
        data = cast(dict[str, object], decoded)
        version = data.get("version")
        if not isinstance(version, int) or version <= 0:
            raise WorkflowError("Constitution version must be a positive integer")
        sections = cls._load_sections(data.get("sections"))
        raw_roles = data.get("roles")
        if not isinstance(raw_roles, dict):
            raise WorkflowError("Constitution roles must be an object")
        roles: dict[WorkflowRole, tuple[str, ...]] = {}
        for raw_role, raw_sections in cast(dict[object, object], raw_roles).items():
            role = enum_role(raw_role)
            names = cls._load_names(raw_sections, "role sections")
            if any(section not in sections for section in names):
                raise WorkflowError(
                    f"Constitution role {role.value} references an unknown section"
                )
            roles[role] = names
        return cls(version, sections, roles)

    @staticmethod
    def _load_sections(raw_sections: object) -> dict[str, tuple[str, ...]]:
        if not isinstance(raw_sections, dict):
            raise WorkflowError("Constitution sections must be an object")
        sections: dict[str, tuple[str, ...]] = {}
        for raw_name, raw_rules in cast(dict[object, object], raw_sections).items():
            if not isinstance(raw_name, str) or not raw_name.strip():
                raise WorkflowError("Constitution section names are required")
            sections[raw_name] = Constitution._load_names(raw_rules, "rules")
        return sections

    @staticmethod
    def _load_names(raw_names: object, label: str) -> tuple[str, ...]:
        if not isinstance(raw_names, list) or not raw_names:
            raise WorkflowError(f"Constitution {label} must be a non-empty list")
        raw_items = cast(list[object], raw_names)
        names = tuple(
            require_text(item, label[:-1] if label.endswith("s") else label, 1_000)
            for item in raw_items
            if isinstance(item, str)
        )
        if len(names) != len(raw_items):
            raise WorkflowError(f"Constitution {label} must contain strings")
        return names

    def rules_for(self, role: WorkflowRole | str) -> tuple[str, ...]:
        normalized = enum_role(role)
        sections = self.roles.get(normalized)
        if sections is None:
            raise WorkflowError(
                f"No constitution rules configured for {normalized.value}"
            )
        return tuple(rule for section in sections for rule in self.sections[section])
