from __future__ import annotations

import json
from typing import cast

from ..review import (
    PullRequestTarget,
    ReaderExecution,
    ReaderStatus,
    ReviewConcern,
)


class GitHubEvidenceReviewReader:
    """Leave verdicts pending until concern-specific analysis is configured."""

    def __init__(self, concern: ReviewConcern) -> None:
        self.concern = concern

    def review(self, target: PullRequestTarget) -> ReaderExecution:
        if target.evidence_json is None:
            return ReaderExecution(
                ReaderStatus.FAIL,
                (f"GitHub review evidence is missing for {self.concern.value}",),
            )
        evidence_value: object = json.loads(target.evidence_json)
        pull_request: object = (
            cast(dict[str, object], evidence_value).get("pull_request")
            if isinstance(evidence_value, dict)
            else None
        )
        identifier = (
            cast(dict[str, object], pull_request).get("id")
            if isinstance(pull_request, dict)
            else None
        )
        if not isinstance(identifier, str) or not identifier:
            message = (
                f"GitHub pull-request evidence is incomplete for {self.concern.value}"
            )
            return ReaderExecution(
                ReaderStatus.FAIL,
                (message,),
            )
        return ReaderExecution(ReaderStatus.PENDING, evidence_refs=(identifier,))
