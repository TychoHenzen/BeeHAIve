from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .handoff_mutation_audit import HandoffMutationAudit

__all__ = ["HandoffRequest"]


@dataclass(frozen=True, slots=True)
class HandoffRequest:
    """Information required to create a branch and pull request."""

    project_id: str
    repository: str
    pbi_number: int
    title: str
    branch: str
    base_branch: str | None
    body: str
    run_id: str
    head_sha: str | None = None
    verification_evidence: str = ""
    mutation_audit: HandoffMutationAudit | None = None
