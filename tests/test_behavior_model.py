from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from beehaiive.behavior import normalize_definition
from beehaiive.behavior_model import OllamaBehaviorModelClient
from tests.test_behavior import definition


class ModelHandler(BaseHTTPRequestHandler):
    response_body = definition()

    def do_POST(self) -> None:
        length = int(self.headers["Content-Length"])
        request = json.loads(self.rfile.read(length))
        assert isinstance(request["format"], dict)
        assert request["format"]["properties"]["schema_version"]["const"] == 1
        payload = json.dumps(
            {"message": {"content": json.dumps(self.response_body)}}
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _format: str, *_args: object) -> None:
        return


@pytest.fixture
def model_server() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), ModelHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_ollama_client_reads_structured_json_from_local_server(
    model_server: str,
) -> None:
    client = OllamaBehaviorModelClient(base_url=model_server, model="test-model")
    assert client.generate("move the item") == normalize_definition(definition())


def test_ollama_client_rejects_non_local_endpoint() -> None:
    with pytest.raises(ValueError, match="local Ollama URL"):
        OllamaBehaviorModelClient(base_url="https://model.example")
