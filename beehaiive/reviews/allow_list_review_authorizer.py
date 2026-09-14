from __future__ import annotations

from collections.abc import Iterable

from .review_action import ReviewAction


class AllowListReviewAuthorizer:
    """Default policy that reserves approval for named human operators."""

    def __init__(
        self,
        human_actors: Iterable[str] = ("operator",),
        reader_actors: Iterable[str] = ("reader", "operator"),
        writer_actors: Iterable[str] = ("writer", "operator"),
    ) -> None:
        self._human_actors = frozenset(human_actors)
        self._reader_actors = frozenset(reader_actors)
        self._writer_actors = frozenset(writer_actors)

    def authorize(
        self, pull_request_id: str, actor: str, action: ReviewAction | str
    ) -> bool:
        del pull_request_id
        normalized_actor = actor.strip()
        if not normalized_actor:
            return False
        try:
            resolved_action = ReviewAction(action)
        except ValueError:
            return False
        allowed_actors = {
            ReviewAction.START: self._writer_actors,
            ReviewAction.READ: self._human_actors
            | self._reader_actors
            | self._writer_actors,
            ReviewAction.READER: self._reader_actors,
            ReviewAction.WRITER: self._writer_actors,
            ReviewAction.PUBLISH: self._writer_actors,
            ReviewAction.APPROVE: self._human_actors,
            ReviewAction.HANDOFF: self._human_actors,
            ReviewAction.REPAIR: self._human_actors,
        }
        return normalized_actor in allowed_actors[resolved_action]
