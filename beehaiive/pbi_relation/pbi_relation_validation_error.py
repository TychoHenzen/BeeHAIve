from __future__ import annotations

from .pbi_relation_error import PbiRelationError

__all__ = ["PbiRelationValidationError"]


class PbiRelationValidationError(PbiRelationError):
    pass
