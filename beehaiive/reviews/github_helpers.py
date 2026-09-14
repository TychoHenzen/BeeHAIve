from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, cast

from ..provider import (
    ProviderError,
    require_graphql_mapping,
)
from ..review import (
    FindingPublicationChannel,
    FindingPublicationState,
    PublicationOutcome,
)


def required_fields(
    value: Mapping[str, Any], fields: tuple[str, ...], label: str
) -> None:
    missing = [field for field in fields if field not in value]
    if missing:
        raise ProviderError(f"GitHub review {label} omitted required fields")


def required_connection_nodes(value: object, label: str) -> list[Mapping[str, Any]]:
    connection = require_graphql_mapping(value)
    raw_nodes = connection.get("nodes")
    if not isinstance(raw_nodes, list):
        raise ProviderError(f"GitHub review {label} returned invalid nodes")
    nodes = cast(list[object], raw_nodes)
    if not all(isinstance(node, Mapping) for node in nodes):
        raise ProviderError(f"GitHub review {label} returned invalid nodes")
    return [cast(Mapping[str, Any], node) for node in nodes]


def optional_mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return cast(Mapping[str, Any], value)


def review_evidence(evidence_json: str | None) -> Mapping[str, Any]:
    if evidence_json is None:
        raise ProviderError("GitHub pull request omitted review evidence")
    try:
        evidence = json.loads(evidence_json)
    except json.JSONDecodeError as exc:
        raise ProviderError("GitHub review evidence is invalid") from exc
    if not isinstance(evidence, Mapping):
        raise ProviderError("GitHub review evidence is invalid")
    return cast(Mapping[str, Any], evidence)


def pull_request_node_id(evidence_json: str | None) -> str:
    pull_request = optional_mapping(review_evidence(evidence_json).get("pull_request"))
    identifier = pull_request.get("id")
    if not isinstance(identifier, str) or not identifier:
        raise ProviderError("GitHub review evidence omitted its pull-request id")
    return identifier


def publication_from_evidence(
    evidence_json: str | None,
    marker: str,
    expected_channel: FindingPublicationChannel,
    remote_id: str | None,
    remote_url: str | None,
) -> PublicationOutcome | None:
    evidence = review_evidence(evidence_json)
    matches: list[tuple[FindingPublicationChannel, str, str]] = []
    for raw_review in evidence.get("reviews", []):
        if not isinstance(raw_review, Mapping):
            continue
        review = cast(Mapping[str, Any], raw_review)
        identifier, url, body = review.get("id"), review.get("url"), review.get("body")
        if (
            isinstance(identifier, str)
            and isinstance(url, str)
            and isinstance(body, str)
            and marker in body
        ):
            matches.append((FindingPublicationChannel.REVIEW_BODY, identifier, url))

    for raw_thread in evidence.get("review_threads", []):
        if not isinstance(raw_thread, Mapping):
            continue
        thread = cast(Mapping[str, Any], raw_thread)
        thread_id = thread.get("id")
        comments = thread.get("comments", [])
        if not isinstance(thread_id, str) or not isinstance(comments, list):
            continue
        for raw_comment in cast(list[object], comments):
            if not isinstance(raw_comment, Mapping):
                continue
            comment = cast(Mapping[str, Any], raw_comment)
            body, url = comment.get("body"), comment.get("url")
            if isinstance(body, str) and marker in body:
                matches.append(
                    (
                        FindingPublicationChannel.REVIEW_THREAD,
                        thread_id,
                        url if isinstance(url, str) else "",
                    )
                )

    if len(matches) > 1:
        return PublicationOutcome(
            FindingPublicationState.DUPLICATE_REMOTE,
            expected_channel,
            remote_id,
            remote_url,
            {
                "reason": "multiple_remote_markers",
                "remote_ids": [match[1] for match in matches[:5]],
            },
        )
    if matches:
        actual_channel, actual_id, actual_url = matches[0]
        if actual_channel is not expected_channel:
            return PublicationOutcome(
                FindingPublicationState.DUPLICATE_REMOTE,
                actual_channel,
                actual_id,
                actual_url,
                {"reason": "remote_marker_channel_mismatch"},
            )
        return PublicationOutcome(
            FindingPublicationState.PUBLISHED,
            actual_channel,
            actual_id,
            actual_url,
        )
    if remote_id is not None:
        return PublicationOutcome(
            FindingPublicationState.REMOTE_MISSING,
            expected_channel,
            remote_id,
            remote_url,
            {"reason": "remote_content_missing"},
        )
    return None
