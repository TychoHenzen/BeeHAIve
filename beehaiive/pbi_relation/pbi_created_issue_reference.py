from __future__ import annotations

from dataclasses import dataclass

__all__ = ["PbiCreatedIssueReference"]


@dataclass(frozen=True, slots=True)
class PbiCreatedIssueReference:
    repository: str
    node_id: str
    number: int
    url: str
    project_item_id: str
