from __future__ import annotations

from urllib.error import HTTPError

import pytest

from beehaiive.github import transport as transport_module
from beehaiive.provider import (
    GitHubRateLimitError,
    UrllibGraphQLClient,
)
from tests.support.edges.fake_response import FakeResponse as FakeResponse


def test_urllib_graphql_client_honors_primary_and_secondary_rate_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary_client = UrllibGraphQLClient("token", "https://example.test/graphql")
    primary_calls = 0

    def primary_response(request: object, timeout: float) -> FakeResponse:
        nonlocal primary_calls
        primary_calls += 1
        return FakeResponse(
            {
                "errors": [
                    {
                        "type": "RATE_LIMIT",
                        "code": "graphql_rate_limit",
                        "message": "API rate limit already exceeded",
                    }
                ]
            },
            {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "4000000000"},
        )

    monkeypatch.setattr(transport_module, "urlopen", primary_response)
    with pytest.raises(GitHubRateLimitError) as primary_error:
        primary_client.execute("query", {})
    assert primary_error.value.primary
    assert primary_error.value.reset_at == 4_000_000_000
    with pytest.raises(GitHubRateLimitError, match="cooldown"):
        primary_client.execute("query", {})
    assert primary_calls == 1

    secondary_client = UrllibGraphQLClient("token", "https://example.test/graphql")
    secondary_calls = 0

    def secondary_response(request: object, timeout: float) -> FakeResponse:
        nonlocal secondary_calls
        secondary_calls += 1
        raise HTTPError(
            "https://example.test/graphql",
            429,
            "too many requests",
            {"retry-after": "60"},
            None,
        )

    monkeypatch.setattr(transport_module, "urlopen", secondary_response)
    with pytest.raises(GitHubRateLimitError) as secondary_error:
        secondary_client.execute("query", {})
    assert not secondary_error.value.primary
    with pytest.raises(GitHubRateLimitError, match="cooldown"):
        secondary_client.execute("query", {})
    assert secondary_calls == 1

    secondary_403_client = UrllibGraphQLClient("token", "https://example.test/graphql")

    def secondary_403_response(request: object, timeout: float) -> object:
        raise HTTPError(
            "https://example.test/graphql",
            403,
            "secondary rate limit",
            {"retry-after": "60"},
            None,
        )

    monkeypatch.setattr(
        transport_module,
        "urlopen",
        secondary_403_response,
    )
    with pytest.raises(GitHubRateLimitError) as secondary_403_error:
        secondary_403_client.execute("query", {})
    assert not secondary_403_error.value.primary

    reset_only_client = UrllibGraphQLClient("token", "https://example.test/graphql")
    monkeypatch.setattr(
        transport_module,
        "urlopen",
        lambda request, timeout: FakeResponse(
            {
                "errors": [
                    {
                        "type": "RATE_LIMITED",
                        "message": "secondary rate limit",
                    }
                ]
            },
            {"x-ratelimit-reset": "4000000000"},
        ),
    )
    with pytest.raises(GitHubRateLimitError) as reset_only_error:
        reset_only_client.execute("query", {})
    assert not reset_only_error.value.primary

    fallback_client = UrllibGraphQLClient("token", "https://example.test/graphql")
    monkeypatch.setattr(
        transport_module,
        "urlopen",
        lambda request, timeout: FakeResponse({"errors": [{"type": "RATE_LIMITED"}]}),
    )
    with pytest.raises(GitHubRateLimitError) as fallback_error:
        fallback_client.execute("query", {})
    assert not fallback_error.value.primary

    exhausted_client = UrllibGraphQLClient("token", "https://example.test/graphql")
    monkeypatch.setattr(
        transport_module,
        "urlopen",
        lambda request, timeout: FakeResponse(
            {"data": {"ok": True}},
            {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "4000000000"},
        ),
    )
    assert exhausted_client.execute("query", {}) == {"ok": True}
    with pytest.raises(GitHubRateLimitError, match="cooldown"):
        exhausted_client.execute("query", {})

    invalid_header_client = UrllibGraphQLClient("token", "https://example.test/graphql")
    monkeypatch.setattr(
        transport_module,
        "urlopen",
        lambda request, timeout: FakeResponse(
            {"data": {"ok": True}},
            {"x-ratelimit-remaining": "unknown"},
        ),
    )
    assert invalid_header_client.execute("query", {}) == {"ok": True}

    class HeaderBag:
        def items(self) -> list[tuple[str, str]]:
            return [("X-RateLimit-Remaining", "10")]

    object_header_client = UrllibGraphQLClient("token", "https://example.test/graphql")
    monkeypatch.setattr(
        transport_module,
        "urlopen",
        lambda request, timeout: FakeResponse({"data": {"ok": True}}, HeaderBag()),
    )
    assert object_header_client.execute("query", {}) == {"ok": True}
