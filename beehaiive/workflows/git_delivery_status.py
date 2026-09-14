from __future__ import annotations

from enum import StrEnum


class GitDeliveryStatus(StrEnum):
    """Result of committing and pushing one leased worktree."""

    PUSHED = "pushed"
    NO_CHANGES = "no_changes"
    BLOCKED = "blocked"
