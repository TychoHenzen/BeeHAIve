from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PullRequestTarget:
    """Current provider-backed pull-request state used by a review run."""

    pull_request_id: str
    head_sha: str
    ready: bool = True
    evidence_json: str | None = None
