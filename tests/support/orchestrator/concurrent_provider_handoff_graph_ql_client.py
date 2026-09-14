from copy import deepcopy
from threading import Barrier, Lock
from typing import Any

from beehaiive.provider import (
    ProviderError,
)
from tests.support.orchestrator.handoff_graph_ql_client import HandoffGraphQLClient


class ConcurrentProviderHandoffGraphQLClient(HandoffGraphQLClient):
    def __init__(self) -> None:
        super().__init__()
        self._state_lock = Lock()
        self._initial_reads = Barrier(2)
        self.repository_reads = 0
        self.ref_create_attempts = 0

    def execute(self, query: str, variables: dict[str, object]) -> dict[str, Any]:
        if "pullRequestCursor" in variables:
            with self._state_lock:
                self.repository_reads += 1
                initial_read = self.repository_reads <= 2
                response = deepcopy(super().execute(query, variables))
            if initial_read:
                self._initial_reads.wait(timeout=5)
            return response
        if "CreateRefInput" in query:
            with self._state_lock:
                self.ref_create_attempts += 1
                if self.ref_exists:
                    raise ProviderError("reference already exists")
                return super().execute(query, variables)
        if "CreatePullRequestInput" in query:
            with self._state_lock:
                if self.pull_requests:
                    raise ProviderError("pull request already exists")
                return super().execute(query, variables)
        if "UpdatePullRequestInput" in query:
            with self._state_lock:
                return super().execute(query, variables)
        return super().execute(query, variables)
