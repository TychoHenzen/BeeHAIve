from __future__ import annotations

from enum import StrEnum


class FindingPublicationState(StrEnum):
    UNPUBLISHED = "unpublished"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    RETRYABLE = "retryable"
    STALE = "stale"
    REMOTE_MISSING = "remote_missing"
    DUPLICATE = "duplicate"
    DUPLICATE_REMOTE = "duplicate_remote"
