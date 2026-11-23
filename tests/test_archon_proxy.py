from __future__ import annotations

from pathlib import Path

import httpx2

from beehaiive.routes.archon_proxy import CALL_TIMEOUT, STREAM_TIMEOUT
from tests.archon_fakes import ACTION, archon_harness


def test_a_read_is_forwarded_with_its_query(tmp_path: Path) -> None:
    def codebases(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200, json=[{"id": "c1"}], headers={"x-internal": "secret"}
        )

    harness = archon_harness(tmp_path, codebases)

    answer = harness.client.get(
        "/archon/api/codebases?limit=5&q=a%20b",
        headers={"accept": "application/json", "cookie": "s=1"},
    )

    assert answer.status_code == 200 and answer.json() == [{"id": "c1"}]
    assert "x-internal" not in answer.headers
    sent = harness.seen[0]
    assert sent.url.path == "/api/codebases"
    assert sent.url.params["q"] == "a b" and sent.url.params["limit"] == "5"
    assert sent.headers["accept"] == "application/json"
    assert "cookie" not in sent.headers
    assert sent.extensions["timeout"] == CALL_TIMEOUT.as_dict()


def test_an_action_is_forwarded_with_its_body_and_status(tmp_path: Path) -> None:
    def cancel(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(409, json={"error": "already done"})

    harness = archon_harness(tmp_path, cancel)

    answer = harness.client.post(
        "/archon/api/workflows/runs/r1/cancel",
        json={"reason": "stop"},
        headers=ACTION,
    )

    assert answer.status_code == 409 and answer.json() == {"error": "already done"}
    sent = harness.seen[0]
    assert sent.method == "POST"
    assert sent.content == b'{"reason":"stop"}'
    assert sent.headers["content-type"] == "application/json"


def test_an_event_stream_is_relayed_as_it_comes(tmp_path: Path) -> None:
    def stream(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=b'data: {"type":"workflow_status"}\n\n',
        )

    harness = archon_harness(tmp_path, stream)

    with harness.client.stream("GET", "/archon/api/stream/__dashboard__") as answer:
        body = b"".join(answer.iter_bytes())

    assert answer.headers["content-type"].startswith("text/event-stream")
    assert answer.headers["cache-control"] == "no-cache"
    assert body == b'data: {"type":"workflow_status"}\n\n'
    assert harness.seen[0].extensions["timeout"] == STREAM_TIMEOUT.as_dict()


def test_archon_being_down_is_a_502(tmp_path: Path) -> None:
    def down(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused", request=request)

    harness = archon_harness(tmp_path, down)

    answer = harness.client.get("/archon/api/health")

    assert answer.status_code == 502
    assert answer.json()["error"] == "Archon's server did not answer."
