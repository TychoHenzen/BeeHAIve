from __future__ import annotations

from enum import StrEnum


class RoutingStatus(StrEnum):
    """Lifecycle state of one active problem."""

    ACTIVE = "active"
    RESOLVED = "resolved"
    HUMAN_HANDOFF = "human_handoff"
