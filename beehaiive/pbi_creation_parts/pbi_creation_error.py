from __future__ import annotations

__all__ = ["PbiCreationError"]


class PbiCreationError(ValueError):
    """A safe, bounded error returned by the PBI creation API."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        status_code: int = 422,
        unknown_outcome: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.unknown_outcome = unknown_outcome
