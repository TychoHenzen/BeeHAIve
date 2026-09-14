from typing import Any


class FakeGraphQLClient:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data

    def execute(self, query: str, variables: dict[str, object]) -> dict[str, Any]:
        return self.data
