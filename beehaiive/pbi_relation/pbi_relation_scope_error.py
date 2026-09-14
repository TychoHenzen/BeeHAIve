from __future__ import annotations

from .pbi_relation_error import PbiRelationError

__all__ = ["PbiRelationScopeError"]


class PbiRelationScopeError(PbiRelationError):
    def __init__(
        self, message: str = "Project or repository is not authorized"
    ) -> None:
        super().__init__(message, code="scope_denied", status_code=403)
