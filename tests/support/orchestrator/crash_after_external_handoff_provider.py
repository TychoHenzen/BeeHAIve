from beehaiive.models import (
    HandoffRequest,
    HandoffResult,
    ProjectSnapshot,
)
from tests.conftest import FakeProvider


class CrashAfterExternalHandoffProvider(FakeProvider):
    def __init__(self, snapshot: ProjectSnapshot) -> None:
        super().__init__(snapshot)
        self.external_artifacts: dict[str, HandoffResult] = {}
        self.fail_once = True
        self.resolved_bases: list[str] = []

    def resolve_base_branch(self, repository: str, requested: str | None) -> str:
        resolved = requested or ("main" if not self.resolved_bases else "develop")
        self.resolved_bases.append(resolved)
        return resolved

    def create_handoff(self, request: HandoffRequest) -> HandoffResult:
        self.handoffs.append(request)
        result = self.external_artifacts.setdefault(
            request.branch,
            HandoffResult(
                request.branch,
                f"https://example.test/{request.repository}/pull/1",
                1,
            ),
        )
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("crashed after external handoff")
        return result
