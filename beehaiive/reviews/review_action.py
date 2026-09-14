from __future__ import annotations

from enum import StrEnum


class ReviewAction(StrEnum):
    """Closed set of actions that the review authorizer may permit."""

    START = "start"
    READ = "read"
    READER = "reader"
    WRITER = "writer"
    PUBLISH = "publish"
    APPROVE = "approve"
    HANDOFF = "handoff"
    REPAIR = "repair"
