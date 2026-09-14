from __future__ import annotations

from dataclasses import dataclass

from .run_state import RunState

__all__ = ["HandoffIntent"]


@dataclass(frozen=True, slots=True)
class HandoffIntent:
    """Request details persisted before an external handoff begins."""

    run: RunState
    branch: str
    base_branch: str | None
    body: str
    head_sha: str | None = None
    verification_evidence: str = ""
