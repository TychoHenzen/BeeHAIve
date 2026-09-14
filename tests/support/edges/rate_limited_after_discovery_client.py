from __future__ import annotations

from typing import Any

from beehaiive.provider import (
    GitHubRateLimitError,
)
from tests.support.edges.static_client import StaticClient


class RateLimitedAfterDiscoveryClient(StaticClient):
    def execute(self, query: str, variables: dict[str, object]) -> dict[str, Any]:
        if len(self.calls) >= 3:
            raise GitHubRateLimitError("limited")
        return super().execute(query, variables)
