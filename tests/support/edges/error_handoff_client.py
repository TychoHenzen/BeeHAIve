from __future__ import annotations

from typing import Any


class ErrorHandoffClient:
    def __init__(
        self,
        *,
        bad_base: bool = False,
        bad_metadata: bool = False,
        bad_pull_request: bool = False,
        bad_ref: bool = False,
        bad_ref_name: bool = False,
    ) -> None:
        self.bad_base = bad_base
        self.bad_metadata = bad_metadata
        self.bad_pull_request = bad_pull_request
        self.bad_ref = bad_ref
        self.bad_ref_name = bad_ref_name

    def execute(self, query: str, variables: dict[str, object]) -> dict[str, Any]:
        if "baseRef:" in query:
            name = "wrong" if self.bad_base else "release"
            return {"repository": {"baseRef": {"name": name, "target": {}}}}
        if "CreatePullRequestInput" in query and self.bad_pull_request:
            return {"createPullRequest": {"pullRequest": {}}}
        if "CreateRefInput" in query and self.bad_ref:
            return {"createRef": {"ref": None}}
        if "CreateRefInput" in query and self.bad_ref_name:
            return {"createRef": {"ref": {"name": "wrong"}}}
        if "CreateRefInput" in query:
            return {
                "createRef": {
                    "ref": {
                        "name": variables["input"]["name"],
                        "target": {"oid": variables["input"]["oid"]},
                    }
                }
            }
        return {
            "repository": {
                "id": "repo-id",
                "defaultBranchRef": {
                    "name": "main",
                    "target": {} if self.bad_metadata else {"oid": "oid"},
                },
                "ref": None
                if self.bad_ref or self.bad_ref_name
                else {
                    "name": variables["qualifiedBranch"],
                    "target": {"oid": "oid"},
                },
                "pullRequests": {
                    "nodes": [],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                },
            }
        }
