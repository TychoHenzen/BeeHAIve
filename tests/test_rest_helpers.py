from __future__ import annotations

import io
import urllib.error
from dataclasses import dataclass
from typing import Any

import pytest

import beehaiive.rest as rest_module
from beehaiive.database import SnapshotDatabase
from beehaiive.rest import (
    GithubResponseError,
    GithubRestClient,
    HttpResponse,
)
from tests.support import UrlopenResponse, raising


@dataclass(frozen=True)
class FailureCase:
    reply: HttpResponse
    match: str
    url: str


FAILURE_CASES = [
    FailureCase(
        HttpResponse(304, {}, ""), "304 without", "https://example.test/missing"
    ),
    FailureCase(
        HttpResponse(500, {}, '{"message":"server broke"}'),
        "server broke",
        "https://example.test/error",
    ),
    FailureCase(
        HttpResponse(200, {}, "not-json"),
        "invalid JSON",
        "https://example.test/invalid",
    ),
]


def test_rest_query_header_and_body_helpers() -> None:
    assert rest_module._with_query("https://example.test/items?a=1", {"b": 2}) == (
        "https://example.test/items?a=1&b=2"
    )
    assert rest_module._with_query("https://example.test/items", None) == (
        "https://example.test/items"
    )
    assert rest_module._normalise_headers({"ETag": "abc"}) == {"etag": "abc"}
    assert rest_module._body_text(b"body") == "body"
    assert rest_module._decode_json(b'{"ok":true}', "url") == {"ok": True}
    with pytest.raises(GithubResponseError, match="invalid JSON"):
        rest_module._decode_json("not-json", "url")


def test_rest_response_message_and_rate_limit_detection() -> None:
    assert rest_module._response_message("") == "empty response"
    assert rest_module._response_message('{"message":"bad"}') == "bad"
    assert rest_module._response_message("plain") == "plain"
    assert rest_module._response_message("[1, 2]") == "[1, 2]"
    assert rest_module._is_rate_limit_response(429, {}, "")
    assert rest_module._is_rate_limit_response(403, {"retry-after": "1"}, "")
    assert rest_module._is_rate_limit_response(403, {}, "rate limit exceeded")
    assert not rest_module._is_rate_limit_response(403, {}, "forbidden")


def test_rest_number_and_link_parsers() -> None:
    assert rest_module._parse_int(" 2 ") == 2
    assert rest_module._parse_int("nope") is None
    assert rest_module._parse_float(" 2.5 ") == 2.5
    assert rest_module._parse_float("nope") is None
    assert rest_module.next_link({}) is None
    assert rest_module.next_link({"Link": '<https://example.test/2>; rel="next"'}) == (
        "https://example.test/2"
    )
    assert rest_module.next_link({"link": "<https://example.test/2>; rel=last"}) is None


@pytest.mark.parametrize(
    "case",
    FAILURE_CASES,
    ids=["304-without-cache", "server-error", "invalid-json"],
)
def test_rest_client_reports_controlled_failures(case: FailureCase) -> None:
    database = SnapshotDatabase(":memory:")
    client = GithubRestClient(
        "token",
        database,
        transport=lambda _url, _headers: case.reply,
    )
    with pytest.raises(GithubResponseError, match=case.match):
        client.get_json(case.url)
    database.close()


def test_urlopen_returns_the_success_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_urlopen(*_args: Any, **_kwargs: Any) -> UrlopenResponse:
        return UrlopenResponse()

    monkeypatch.setattr(
        rest_module.urllib.request,
        "urlopen",
        fake_urlopen,
    )
    success = rest_module._urlopen("https://example.test", {})
    assert success.status_code == 200
    assert success.body == b"{}"


def test_urlopen_returns_the_http_error_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = urllib.error.HTTPError(
        "https://example.test",
        403,
        "forbidden",
        {"X-Error": "yes"},
        io.BytesIO(b"forbidden"),
    )
    monkeypatch.setattr(
        rest_module.urllib.request,
        "urlopen",
        raising(error),
    )
    failed = rest_module._urlopen("https://example.test", {})
    assert failed.status_code == 403
    assert failed.body == b"forbidden"
