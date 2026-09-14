from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from .review_action import ReviewAction


class ReviewAuthorizer(Protocol):
    """Authorization policy for review actions."""

    def authorize(
        self, pull_request_id: str, actor: str, action: ReviewAction | str
    ) -> bool: ...
