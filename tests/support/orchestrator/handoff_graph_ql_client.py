from typing import Any


class HandoffGraphQLClient:
    def __init__(self) -> None:
        self.ref_exists = False
        self.branch_sha = "base-oid"
        self.pull_requests: list[dict[str, object]] = []
        self.ref_creations = 0
        self.ref_oids: list[object] = []
        self.pull_request_creations = 0
        self.pull_request_updates = 0
        self.pull_request_bases: list[object] = []
        self.created_pull_request_inputs: list[dict[str, object]] = []
        self.updated_pull_request_inputs: list[dict[str, object]] = []

    def execute(self, query: str, variables: dict[str, object]) -> dict[str, Any]:
        if "baseRef:" in query:
            return {
                "repository": {
                    "baseRef": {
                        "name": "release",
                        "target": {"oid": "release-oid"},
                    }
                }
            }
        if "CreateRefInput" in query:
            self.ref_exists = True
            self.ref_creations += 1
            self.ref_oids.append(variables["input"]["oid"])
            self.branch_sha = str(variables["input"]["oid"])
            return {
                "createRef": {
                    "ref": {
                        "name": variables["input"]["name"],
                        "target": {"oid": self.branch_sha},
                    }
                }
            }
        if "CreatePullRequestInput" in query:
            self.pull_request_creations += 1
            self.pull_request_bases.append(variables["input"]["baseRefName"])
            self.created_pull_request_inputs.append(variables["input"])
            pull_request = {
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
            self.pull_requests.append(pull_request)
            return {"createPullRequest": {"pullRequest": pull_request}}
        if "UpdatePullRequestInput" in query:
            self.pull_request_updates += 1
            self.updated_pull_request_inputs.append(variables["input"])
            pull_request = next(
                item
                for item in self.pull_requests
                if item.get("id") == variables["input"]["pullRequestId"]
            )
            pull_request["title"] = variables["input"]["title"]
            pull_request["body"] = variables["input"]["body"]
            return {"updatePullRequest": {"pullRequest": pull_request}}
        return {
            "repository": {
                "id": "repo-id",
                "defaultBranchRef": {
                    "name": "main",
                    "target": {"oid": "base-oid"},
                },
                "ref": {
                    "name": variables["qualifiedBranch"],
                    "target": {"oid": self.branch_sha},
                }
                if self.ref_exists
                else None,
                "pullRequests": {
                    "nodes": self.pull_requests,
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                },
            }
        }
