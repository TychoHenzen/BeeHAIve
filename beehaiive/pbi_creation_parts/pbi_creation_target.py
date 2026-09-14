from __future__ import annotations

from dataclasses import dataclass

__all__ = ["PbiCreationTarget"]


@dataclass(frozen=True, slots=True)
class PbiCreationTarget:
    project_node_id: str
    repository_node_id: str
    status_field_id: str
    backlog_option_id: str
    backlog_status: str
    label_ids: tuple[str, ...]
