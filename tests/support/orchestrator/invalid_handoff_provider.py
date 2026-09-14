from beehaiive.provider import (
    ProviderError,
)
from tests.conftest import FakeProvider


class InvalidHandoffProvider(FakeProvider):
    def validate_handoff(
        self, repository: str, branch: str, requested_base: str | None
    ) -> str:
        raise ProviderError("invalid handoff metadata")
