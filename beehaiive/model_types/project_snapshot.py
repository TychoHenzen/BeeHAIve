from __future__ import annotations

from dataclasses import dataclass

from .repository_snapshot import RepositorySnapshot

__all__ = ["ProjectSnapshot"]


@dataclass(frozen=True, slots=True)
class ProjectSnapshot:
    """A selected project with all linked repositories."""

    project_id: str
    name: str
    repositories: tuple[RepositorySnapshot, ...]
