from typing import Any

from beehaiive.provider import (
    ProviderError,
)
from tests.support.orchestrator.handoff_graph_ql_client import HandoffGraphQLClient


class RacingHandoffGraphQLClient(HandoffGraphQLClient):
    def __init__(self) -> None:
        super().__init__()
        self.fail_ref_once = True
        self.fail_pull_request_once = True
        self.events: list[str] = []
        self.ref_create_attempts = 0
        self.pull_request_create_attempts = 0

    def execute(self, query: str, variables: dict[str, object]) -> dict[str, Any]:
        if "pullRequestCursor" in variables:
            self.events.append("read")
        if "CreateRefInput" in query:
            self.events.append("create-ref")
            self.ref_create_attempts += 1
            if self.fail_ref_once:
                self.fail_ref_once = False
                self.ref_exists = True
                self.ref_creations += 1
                raise ProviderError("reference creation timed out after commit")
        if "CreatePullRequestInput" in query:
            self.events.append("create-pull-request")
            self.pull_request_create_attempts += 1
            if self.fail_pull_request_once:
                self.fail_pull_request_once = False
                self.pull_request_creations += 1
                self.pull_requests.append(
                    {
                        "id": "pull-request-node-8",
                        "number": 8,
                        "url": "https://example.test/owner/api/pull/8",
                        "title": variables["input"]["title"],
                        "state": "OPEN",
                        "isDraft": variables["input"]["draft"],
                        "headRefName": variables["input"]["headRefName"],
                        "headRefOid": self.branch_sha,
                        "baseRefName": variables["input"]["baseRefName"],
                        "body": variables["input"]["body"],
                    }
                )
                raise ProviderError("pull request creation timed out after commit")
        if "UpdatePullRequestInput" in query:
            self.events.append("update-pull-request")
        return super().execute(query, variables)
