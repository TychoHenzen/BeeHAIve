from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .pbi_relation_issue import PbiRelationIssue

__all__ = ["PbiRelationSnapshot"]


@dataclass(frozen=True, slots=True)
class PbiRelationSnapshot:
    parent: PbiRelationIssue
    children: tuple[PbiRelationIssue, ...]
    parent_sub_issues: tuple[PbiRelationIssue, ...]
    parent_by_child: Mapping[int, int | None]
