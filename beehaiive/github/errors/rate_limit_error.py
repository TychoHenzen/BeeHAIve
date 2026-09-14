from __future__ import annotations

from beehaiive.github.errors.provider_error import ProviderError as ProviderError


class GitHubRateLimitError(ProviderError):
    """Raised when GitHub asks the client to stop GraphQL requests temporarily."""

    def __init__(
        self,
        message: str,
        *,
        reset_at: float | None = None,
        retry_after: float | None = None,
        primary: bool = False,
        remaining: float | None = None,
    ) -> None:
        super().__init__(message)
        self.reset_at = reset_at
        self.retry_after = retry_after
        self.primary = primary
        self.remaining = remaining


__all__ = ["GitHubRateLimitError"]
