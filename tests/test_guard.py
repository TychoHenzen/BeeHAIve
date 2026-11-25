from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import Headers

from beehaiive.guard import host_name, refusal
from tests.archon_fakes import ACTION, archon_harness


@pytest.mark.parametrize(
    ("value", "host"),
    [
        ("127.0.0.1:8000", "127.0.0.1"),
        ("LOCALHOST", "localhost"),
        ("[::1]:8000", "::1"),
        ("evil.example", "evil.example"),
        ("", ""),
    ],
)
def test_host_name_drops_the_port(value: str, host: str) -> None:
    assert host_name(value) == host


def test_reads_from_this_machine_pass() -> None:
    assert refusal("GET", Headers({"host": "127.0.0.1:8000"})) is None
    assert refusal("GET", Headers({"host": "localhost"})) is None


def test_a_foreign_host_is_refused_even_for_reads() -> None:
    # A rebound DNS name points at 127.0.0.1 but keeps its own Host header.
    assert refusal("GET", Headers({"host": "attacker.example:8000"})) is not None


def test_a_foreign_origin_is_refused() -> None:
    headers = Headers({"host": "127.0.0.1", "origin": "https://attacker.example"})

    assert refusal("GET", headers) == "That page is not BeeHAIve."


def test_an_action_needs_the_header_the_page_sends() -> None:
    plain = Headers({"host": "127.0.0.1", "origin": "http://127.0.0.1:8000"})
    # Spelled exactly as web/console/api.js sends it.
    marked = Headers({"host": "127.0.0.1", "X-BeeHAIve": "1"})

    assert refusal("POST", plain) == "An action must come from BeeHAIve's page."
    assert refusal("POST", marked) is None


def test_the_guard_answers_403_before_any_route(tmp_path: Path) -> None:
    harness = archon_harness(tmp_path)
    foreign = TestClient(harness.client.app, base_url="http://attacker.example")

    assert foreign.get("/healthz").status_code == 403
    refused = harness.client.post("/api/archon/triggers/bindings/x/fire")
    assert refused.status_code == 403
    assert "BeeHAIve's page" in refused.json()["error"]
    allowed = harness.client.post(
        "/api/archon/triggers/bindings/x/fire", headers=ACTION
    )
    assert allowed.status_code == 404
