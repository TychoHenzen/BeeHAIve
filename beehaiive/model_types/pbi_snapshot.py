from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from .helpers import _empty_metadata
from .stage import Stage

__all__ = ["PbiSnapshot"]


@dataclass(frozen=True, slots=True)
class PbiSnapshot:
    """A PBI discovered from a project provider."""

    repository: str
    number: int
    title: str
    stage: Stage | None = Stage.BACKLOG
    planning_status: str | None = None
    claimable: bool = True
    metadata: Mapping[str, object] = field(default_factory=_empty_metadata)
