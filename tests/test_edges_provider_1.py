from __future__ import annotations

from urllib.error import HTTPError, URLError

import pytest

import beehaiive.provider as provider_module
from beehaiive.github import transport as transport_module
from beehaiive.provider import (
    GitHubOutcomeUnknownError,
    GitHubRateLimitError,
    ProviderError,
    UrllibGraphQLClient,
)
from tests.support.edges.fake_response import FakeResponse as FakeResponse


def test_urllib_graphql_client_validates_transport_and_payloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = UrllibGraphQLClient("token", "https://example.test/graphql")

    monkeypatch.setattr(
        transport_module,
        "urlopen",
        lambda request, timeout: FakeResponse({"data": {"ok": True}}),
    )
    assert client.execute("query", {}) == {"ok": True}

    for error in (
        HTTPError("https://example.test", 400, "failed", {}, None),
        URLError("offline"),
        TimeoutError("timeout"),
        OSError("reset"),
    ):

        def raise_error(
            request: object, timeout: int, error: BaseException = error
        ) -> object:
            raise error

        monkeypatch.setattr(transport_module, "urlopen", raise_error)
        with pytest.raises(ProviderError, match="request failed") as request_error:
            client.execute("query", {})
        if isinstance(error, HTTPError):
            assert not isinstance(request_error.value, GitHubOutcomeUnknownError)
        else:
            assert isinstance(request_error.value, GitHubOutcomeUnknownError)

    def raise_server_error(request: object, timeout: float) -> object:
        raise HTTPError("https://example.test", 503, "unavailable", {}, None)

    monkeypatch.setattr(transport_module, "urlopen", raise_server_error)
    with pytest.raises(
        GitHubOutcomeUnknownError, match="request failed"
    ) as server_error:
        client.execute("mutation", {})
    assert server_error.value.status_code == 503

    for payload, message in (
        ([], "non-object"),
        ({"errors": ["bad"]}, "returned errors"),
        ({"errors": [{"type": "OTHER"}]}, "returned errors"),
        ({"data": []}, "did not contain data"),
    ):
        monkeypatch.setattr(
            transport_module,
            "urlopen",
            lambda request, timeout, payload=payload: FakeResponse(payload),
        )
        with pytest.raises(ProviderError, match=message):
            client.execute("query", {})

    monkeypatch.setattr(
        transport_module,
        "urlopen",
        lambda request, timeout: FakeResponse(b"not-json"),
    )
    with pytest.raises(ProviderError, match="invalid JSON") as json_error:
        client.execute("query", {})
    assert isinstance(json_error.value, GitHubOutcomeUnknownError)

    monkeypatch.setattr(
        transport_module,
        "urlopen",
        lambda request, timeout: FakeResponse(b"\xff"),
    )
    with pytest.raises(ProviderError, match="invalid JSON") as unicode_error:
        client.execute("query", {})
    assert isinstance(unicode_error.value, GitHubOutcomeUnknownError)


def test_rate_limit_wait_respects_headers_and_secondary_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(provider_module.time, "time", lambda: 1_000)

    assert (
        provider_module._rate_limit_wait_seconds(
            GitHubRateLimitError("primary", reset_at=1_040, primary=True),
            previous_secondary_wait=None,
        )
        == 40
    )
    assert (
        provider_module._rate_limit_wait_seconds(
            GitHubRateLimitError("primary", reset_at=900, retry_after=7, primary=True),
            previous_secondary_wait=None,
        )
        == 7
    )
    assert (
        provider_module._rate_limit_wait_seconds(
            GitHubRateLimitError(
                "secondary", reset_at=1_040, retry_after=3, primary=False, remaining=7
            ),
            previous_secondary_wait=None,
        )
        == 3
    )
    assert (
        provider_module._rate_limit_wait_seconds(
            GitHubRateLimitError(
                "secondary", retry_after=3, primary=False, remaining=7
            ),
            previous_secondary_wait=3,
        )
        == 6
    )
    assert (
        provider_module._rate_limit_wait_seconds(
            GitHubRateLimitError(
                "secondary", reset_at=1_040, primary=False, remaining=0
            ),
            previous_secondary_wait=None,
        )
        == 40
    )
    assert (
        provider_module._rate_limit_wait_seconds(
            GitHubRateLimitError("secondary", reset_at=900, primary=False, remaining=0),
            previous_secondary_wait=None,
        )
        == 60
    )
    assert (
        provider_module._rate_limit_wait_seconds(
            GitHubRateLimitError(
                "secondary", reset_at=1_040, primary=False, remaining=7
            ),
            previous_secondary_wait=None,
        )
        == 60
    )
