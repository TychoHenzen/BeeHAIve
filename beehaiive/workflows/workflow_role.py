from __future__ import annotations

from enum import StrEnum


class WorkflowRole(StrEnum):
    """Roles that can participate in a committed workflow handoff."""

    PLANNER = "planner"
    WRITER = "writer"
    REVIEWER = "reviewer"
    OPERATOR = "operator"
