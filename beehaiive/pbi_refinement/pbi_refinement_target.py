from __future__ import annotations

from dataclasses import dataclass

__all__ = ["PbiRefinementTarget"]


@dataclass(frozen=True, slots=True)
class PbiRefinementTarget:
    project_node_id: str
    status_field_id: str
    backlog_option_id: str
    backlog_status: str
    todo_option_id: str
    todo_status: str
