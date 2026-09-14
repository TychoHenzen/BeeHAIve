from __future__ import annotations

from .pbi_relation_apply_mixin import PbiRelationApplyMixin
from .pbi_relation_failure_mixin import PbiRelationFailureMixin
from .pbi_relation_provider import PbiRelationProvider


class PbiRelationService(PbiRelationApplyMixin, PbiRelationFailureMixin):
    def __init__(self, provider: PbiRelationProvider) -> None:
        self._provider = provider


__all__ = ["PbiRelationService"]
