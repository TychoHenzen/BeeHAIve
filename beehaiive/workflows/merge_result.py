from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MergeResult:
    """Result of integrating the exact target commit into a repair worktree."""

    conflicted: bool
    evidence: str
