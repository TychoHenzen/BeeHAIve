from beehaiive.models import (
    HandoffRequest,
    HandoffResult,
    ProjectSnapshot,
)
from beehaiive.provider import ProviderError


class ErrorProvider:
    def discover_project(self, project_id: str) -> ProjectSnapshot:
        raise ProviderError(f"cannot discover {project_id}")

    def create_handoff(self, request: HandoffRequest) -> HandoffResult:
        raise ProviderError("cannot hand off")

    def resolve_base_branch(self, repository: str, requested: str | None) -> str:
        raise ProviderError("cannot resolve base branch")

    def validate_handoff(
        self, repository: str, branch: str, requested_base: str | None
    ) -> str:
        raise ProviderError("cannot validate handoff")
