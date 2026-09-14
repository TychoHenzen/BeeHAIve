from __future__ import annotations

from beehaiive.provider import UrllibGraphQLClient


def test_rest_client_accepts_native_relation_array_responses(monkeypatch) -> None:
    class Response:
        status = 200
        headers: dict[str, str] = {}

        def read(self) -> bytes:
            return b"[]"

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *args: object) -> None:
            del args

    monkeypatch.setattr(
        "beehaiive.github.transport.urlopen", lambda *_args, **_kwargs: Response()
    )
    status, payload = UrllibGraphQLClient("not-returned").request_rest(
        "GET", "/repos/owner/repo/issues/1/sub_issues"
    )

    assert status == 200
    assert payload == []
