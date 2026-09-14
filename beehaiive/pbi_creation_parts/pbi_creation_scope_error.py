from __future__ import annotations

from .pbi_creation_error import PbiCreationError

__all__ = ["PbiCreationScopeError"]


class PbiCreationScopeError(PbiCreationError):
    """Raised when a request targets a project or repository outside scope."""

    def __init__(
        self, message: str = "Project or repository is not authorized"
    ) -> None:
        super().__init__(message, code="scope_rejected", status_code=403)
