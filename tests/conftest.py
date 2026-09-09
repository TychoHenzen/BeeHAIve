from beehaiive.models import HandoffRequest, HandoffResult, ProjectSnapshot


class FakeProvider:
    def __init__(self, snapshot: ProjectSnapshot) -> None:
        self.snapshot = snapshot
        self.handoffs: list[HandoffRequest] = []
        self.discoveries = 0

    def discover_project(self, project_id: str) -> ProjectSnapshot:
        assert project_id == self.snapshot.project_id
        self.discoveries += 1
        return self.snapshot

    def create_handoff(self, request: HandoffRequest) -> HandoffResult:
        self.handoffs.append(request)
        return HandoffResult(
            branch=request.branch,
            pull_request_url=f"https://example.test/{request.repository}/pull/1",
            pull_request_number=1,
        )

    def resolve_base_branch(self, repository: str, requested: str | None) -> str:
        return requested or "master"

    def validate_handoff(
        self, repository: str, branch: str, requested_base: str | None
    ) -> str:
        return self.resolve_base_branch(repository, requested_base)
