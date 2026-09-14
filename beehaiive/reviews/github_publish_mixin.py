from __future__ import annotations

from typing import Any

from ..provider import (
    ProviderError,
)
from ..review import (
    FindingPublicationChannel,
    FindingPublicationState,
    PublicationOutcome,
    ReviewConcern,
    finding_publication_marker,
)
from .github_helpers import (
    optional_mapping,
    publication_from_evidence,
    pull_request_node_id,
)
from .github_queries import PUBLISH_PULL_REQUEST_REVIEW_MUTATION


class GithubPublishMixin:
    def publish_finding(
        self: Any,
        pull_request_id: str,
        *,
        expected_head_sha: str,
        fingerprint: str,
        concern: ReviewConcern | str,
        summary: str,
        file_path: str | None = None,
        start_line: int | None = None,
        end_line: int | None = None,
        remote_id: str | None = None,
        remote_url: str | None = None,
    ) -> PublicationOutcome:
        try:
            resolved_concern = ReviewConcern(concern)
        except ValueError as exc:
            raise ProviderError("Review finding has an invalid concern") from exc
        if not summary.strip() or len(summary) > 1_000:
            raise ProviderError("Review finding summary is invalid")
        if file_path is None:
            if start_line is not None or end_line is not None:
                raise ProviderError("Review finding anchor is incomplete")
        elif (
            not file_path.strip()
            or file_path.startswith("/")
            or "\\" in file_path
            or any(part in {"", ".", ".."} for part in file_path.split("/"))
            or type(start_line) is not int
            or type(end_line) is not int
            or start_line < 1
            or end_line < start_line
        ):
            raise ProviderError("Review finding anchor is invalid")

        expected_head_sha = expected_head_sha.strip()
        marker = finding_publication_marker(
            pull_request_id, expected_head_sha, fingerprint
        )
        channel = (
            FindingPublicationChannel.REVIEW_THREAD
            if file_path is not None
            else FindingPublicationChannel.REVIEW_BODY
        )
        target = self.get_pull_request(pull_request_id)
        if not target.ready or target.head_sha != expected_head_sha:
            return PublicationOutcome(
                FindingPublicationState.STALE,
                channel,
                remote_id,
                remote_url,
                {"reason": "current_head_mismatch"},
            )

        existing = publication_from_evidence(
            target.evidence_json, marker, channel, remote_id, remote_url
        )
        if existing is not None:
            return existing

        redacted_summary = self._redact(summary.strip())
        if not isinstance(redacted_summary, str):
            raise ProviderError("Review finding summary could not be redacted")
        review_text = f"[{resolved_concern.value}] {redacted_summary}\n\n{marker}"
        review_input: dict[str, object] = {
            "pullRequestId": pull_request_node_id(target.evidence_json),
            "commitOID": expected_head_sha,
            "event": "COMMENT",
        }
        if channel is FindingPublicationChannel.REVIEW_BODY:
            review_input["body"] = review_text
        else:
            assert (
                file_path is not None
                and start_line is not None
                and end_line is not None
            )
            thread: dict[str, object] = {
                "body": review_text,
                "path": file_path,
                "line": end_line,
                "side": "RIGHT",
            }
            if start_line != end_line:
                thread["startLine"] = start_line
                thread["startSide"] = "RIGHT"
            review_input["threads"] = [thread]

        data = self._configured_client().execute(
            PUBLISH_PULL_REQUEST_REVIEW_MUTATION, {"input": review_input}
        )
        mutation = optional_mapping(data.get("addPullRequestReview"))
        review = optional_mapping(mutation.get("pullRequestReview"))
        created_id = review.get("id")
        created_url = review.get("url")
        if not isinstance(created_id, str) or not created_id:
            raise ProviderError("GitHub review mutation omitted its review id")
        if not isinstance(created_url, str) or not created_url:
            raise ProviderError("GitHub review mutation omitted its review URL")

        confirmed = self.get_pull_request(pull_request_id)
        reconciled = publication_from_evidence(
            confirmed.evidence_json, marker, channel, created_id, created_url
        )
        if not confirmed.ready or confirmed.head_sha != expected_head_sha:
            return PublicationOutcome(
                FindingPublicationState.STALE,
                channel,
                None if reconciled is None else reconciled.remote_id,
                created_url if reconciled is None else reconciled.remote_url,
                {"reason": "current_head_changed_after_publication"},
            )
        if (
            reconciled is None
            or reconciled.state is not FindingPublicationState.PUBLISHED
        ):
            raise ProviderError("GitHub did not confirm the published finding")
        return reconciled
