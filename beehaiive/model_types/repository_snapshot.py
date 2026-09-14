from __future__ import annotations

from dataclasses import dataclass

from .pbi_snapshot import PbiSnapshot

__all__ = ["RepositorySnapshot"]


@dataclass(frozen=True, slots=True)
class RepositorySnapshot:
    """A linked repository and its discovered PBIs."""

    name: str
    pbis: tuple[PbiSnapshot, ...] = ()
