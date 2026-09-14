from __future__ import annotations

from beehaiive.github.errors.provider_error import ProviderError as ProviderError


class GitHubOutcomeUnknownError(ProviderError):
    """Raised when a request may have reached GitHub without a usable reply."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


__all__ = ["GitHubOutcomeUnknownError"]
