from __future__ import annotations

from dataclasses import dataclass

from .models import ProjectCard


@dataclass(frozen=True)
class HeldItem:
    card: ProjectCard
    status: str
