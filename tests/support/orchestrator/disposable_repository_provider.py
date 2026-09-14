import subprocess
from pathlib import Path

from beehaiive.models import (
    HandoffRequest,
    HandoffResult,
    ProjectSnapshot,
)
from tests.conftest import FakeProvider


class DisposableRepositoryProvider(FakeProvider):
    def __init__(self, repository_path: Path, snapshot: ProjectSnapshot) -> None:
        super().__init__(snapshot)
        self.repository_path = repository_path
        self.pull_request_records: list[dict[str, str]] = []

    def create_handoff(self, request: HandoffRequest) -> HandoffResult:
        subprocess.run(
            ["git", "branch", request.branch],
            cwd=self.repository_path,
            check=True,
            capture_output=True,
            text=True,
        )
        pull_request_url = "https://example.test/owner/api/pull/1"
        self.pull_request_records.append(
            {"branch": request.branch, "url": pull_request_url}
        )
        return HandoffResult(request.branch, pull_request_url, 1)
