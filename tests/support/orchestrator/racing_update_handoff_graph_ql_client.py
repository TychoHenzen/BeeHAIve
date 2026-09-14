from typing import Any

from beehaiive.provider import (
    ProviderError,
)
from tests.support.orchestrator.handoff_graph_ql_client import HandoffGraphQLClient


class RacingUpdateHandoffGraphQLClient(HandoffGraphQLClient):
    def __init__(self) -> None:
        super().__init__()
        self.fail_update_once = True
        self.update_attempts = 0

    def execute(self, query: str, variables: dict[str, object]) -> dict[str, Any]:
        if "UpdatePullRequestInput" in query:
            self.update_attempts += 1
            if self.fail_update_once:
                self.fail_update_once = False
                raise ProviderError("pull request changed during update")
        return super().execute(query, variables)
