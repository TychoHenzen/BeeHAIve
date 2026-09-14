from beehaiive.models import (
    ProjectSnapshot,
)
from beehaiive.provider import ProviderError
from tests.conftest import FakeProvider


class FailingDashboardProvider(FakeProvider):
    def discover_project(self, project_id: str) -> ProjectSnapshot:
        raise ProviderError("dashboard provider unavailable")
