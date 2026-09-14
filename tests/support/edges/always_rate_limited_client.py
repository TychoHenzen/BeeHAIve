from __future__ import annotations

from typing import Any

from beehaiive.provider import (
    GitHubRateLimitError,
)


class AlwaysRateLimitedClient:
    def execute(self, query: str, variables: dict[str, object]) -> dict[str, Any]:
        raise GitHubRateLimitError("limited")
