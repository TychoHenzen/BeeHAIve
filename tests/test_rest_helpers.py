from __future__ import annotations

import io
import urllib.error

import pytest

import beehaiive.rest as rest_module
from beehaiive.database import SnapshotDatabase
from beehaiive.rest import (
    GithubResponseError,
    GithubRestClient,
    HttpResponse,
)


def test_rest_helpers_and_controlled_failures() -> None:
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
    assert rest_module._response_message("") == "empty response"
    assert rest_module._response_message('{"message":"bad"}') == "bad"
    assert rest_module._response_message("plain") == "plain"
    assert rest_module._response_message("[1, 2]") == "[1, 2]"
    assert rest_module._is_rate_limit_response(429, {}, "")
    assert rest_module._is_rate_limit_response(403, {"retry-after": "1"}, "")
    assert rest_module._is_rate_limit_response(403, {}, "rate limit exceeded")
    assert not rest_module._is_rate_limit_response(403, {}, "forbidden")
    assert rest_module._parse_int(" 2 ") == 2
    assert rest_module._parse_int("nope") is None
    assert rest_module._parse_float(" 2.5 ") == 2.5
    assert rest_module._parse_float("nope") is None
    assert rest_module.next_link({}) is None
    assert rest_module.next_link({"Link": '<https://example.test/2>; rel="next"'}) == (
        "https://example.test/2"
    )
    assert rest_module.next_link({"link": "<https://example.test/2>; rel=last"}) is None

    database = SnapshotDatabase(":memory:")
    client = GithubRestClient(
        "token",
        database,
        transport=lambda _url, _headers: HttpResponse(304, {}, ""),
    )
    with pytest.raises(GithubResponseError, match="304 without"):
        client.get_json("https://example.test/missing")
    client = GithubRestClient(
        "token",
        database,
        transport=lambda _url, _headers: HttpResponse(
            500, {}, '{"message":"server broke"}'
        ),
    )
    with pytest.raises(GithubResponseError, match="server broke"):
        client.get_json("https://example.test/error")
    client = GithubRestClient(
        "token",
        database,
        transport=lambda _url, _headers: HttpResponse(200, {}, "not-json"),
    )
    with pytest.raises(GithubResponseError, match="invalid JSON"):
        client.get_json("https://example.test/invalid")
    database.close()


def test_rest_urlopen_handles_success_and_http_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Response:
        status = 200
        headers = {"ETag": "one"}

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return b"{}"

    monkeypatch.setattr(
        rest_module.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: Response(),
    )
    success = rest_module._urlopen("https://example.test", {})
    assert success.status_code == 200
    assert success.body == b"{}"

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
        lambda *_args, **_kwargs: (_ for _ in ()).throw(error),
    )
    failed = rest_module._urlopen("https://example.test", {})
    assert failed.status_code == 403
    assert failed.body == b"forbidden"
