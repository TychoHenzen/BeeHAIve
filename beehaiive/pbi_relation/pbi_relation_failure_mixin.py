from __future__ import annotations

from .pbi_relation_request import PbiRelationRequest
from .pbi_relation_result import PbiRelationResult


class PbiRelationFailureMixin:
    @staticmethod
    def _incomplete(
        request: PbiRelationRequest, *, failure_code: str, pending_step: str
    ) -> PbiRelationResult:
        children = tuple(child.number for child in request.children)
        return PbiRelationResult(
            "incomplete",
            None,
            request.parent_issue_number,
            children,
            (),
            (),
            children,
            request.dependencies,
            (),
            (),
            request.dependencies,
            False,
            False,
            (),
            pending_step,
            failure_code,
        )


__all__ = ["PbiRelationFailureMixin"]
