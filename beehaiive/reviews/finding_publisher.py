from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from .publication_outcome import PublicationOutcome
    from .review_concern import ReviewConcern


class FindingPublisher(Protocol):
    def publish_finding(
        self,
        pull_request_id: str,
        *,
        expected_head_sha: str,
        fingerprint: str,
        concern: ReviewConcern,
        summary: str,
        file_path: str | None = None,
        start_line: int | None = None,
        end_line: int | None = None,
        remote_id: str | None = None,
        remote_url: str | None = None,
    ) -> PublicationOutcome: ...
