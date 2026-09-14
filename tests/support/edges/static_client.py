from __future__ import annotations

from typing import Any


class StaticClient:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data
        self.calls: list[str] = []

    def execute(self, query: str, variables: dict[str, object]) -> dict[str, Any]:
        self.calls.append(query)
        return self.data
