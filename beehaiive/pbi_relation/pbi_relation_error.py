from __future__ import annotations

__all__ = ["PbiRelationError"]


class PbiRelationError(ValueError):
    def __init__(self, message: str, *, code: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
