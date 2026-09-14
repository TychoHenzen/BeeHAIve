from typing import Any

from beehaiive.provider import (
    ProviderError,
)
from tests.support.orchestrator.racing_handoff_graph_ql_client import (
    RacingHandoffGraphQLClient,
)


class WrongTargetRacingHandoffGraphQLClient(RacingHandoffGraphQLClient):
    def execute(self, query: str, variables: dict[str, object]) -> dict[str, Any]:
        if "CreateRefInput" in query and self.fail_ref_once:
            self.fail_ref_once = False
            self.ref_exists = True
            self.branch_sha = "unexpected-oid"
            raise ProviderError("reference creation timed out")
        return super().execute(query, variables)
