from __future__ import annotations

from dataclasses import dataclass

__all__ = ["HandoffResult"]


@dataclass(frozen=True, slots=True)
class HandoffResult:
    """The provider's durable branch and pull-request identifiers."""

    branch: str
    pull_request_url: str
    pull_request_number: int | None = None
