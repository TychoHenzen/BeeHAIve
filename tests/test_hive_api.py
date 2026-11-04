from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response

from beehaiive.app import create_app
from tests.hive_support import SeededHive, restore_active_pass, seed_database
from tests.support import skills_config


@pytest.fixture
def seeded_hive(tmp_path: Path) -> Iterator[SeededHive]:
    seeded = seed_database(tmp_path)
    yield seeded
    seeded.database.close()


@pytest.fixture
def client(tmp_path: Path, seeded_hive: SeededHive) -> Iterator[TestClient]:
    database, agent_id, _alert_pass_id, _now = seeded_hive
    app = create_app(skills_config(tmp_path), database=database)
    database.transaction(lambda connection: restore_active_pass(connection, agent_id))
    with TestClient(app) as test_client:
        yield test_client


def acknowledge(client: TestClient, pass_id: str, acknowledged_by: str) -> Response:
    return client.post(
        f"/api/passes/{pass_id}/acknowledge",
        json={"acknowledged_by": acknowledged_by},
    )


def test_hive_http_events_replay_nothing_after_the_cursor(client: TestClient) -> None:
    hive = client.get("/api/hive")
    events = client.get("/api/hive/events?since=0")
    cursor = events.json()["cursor"]
    replay = client.get("/api/hive/events", params={"since": cursor})

    assert hive.status_code == 200
    assert events.status_code == 200
    assert events.json()["events"]
    assert replay.json()["events"] == []


def test_hive_http_acknowledgement_rejects_forged_and_missing_alerts(
    client: TestClient, seeded_hive: SeededHive
) -> None:
    _database, _agent_id, alert_pass_id, _now = seeded_hive
    forged = acknowledge(client, alert_pass_id, "forged")
    no_alert = acknowledge(client, "finished-2", "operator")

    assert forged.status_code == 403
    assert no_alert.status_code == 409


def test_hive_http_acknowledgement_clears_the_alert_and_records_the_operator(
    client: TestClient, seeded_hive: SeededHive
) -> None:
    _database, agent_id, alert_pass_id, _now = seeded_hive
    forged = acknowledge(client, alert_pass_id, "forged")
    acknowledged = acknowledge(client, alert_pass_id, "operator")
    after_ack = client.get("/api/hive")
    history = client.get(f"/api/agents/{agent_id}/history")

    assert forged.status_code == 403
    assert acknowledged.status_code == 200
    assert acknowledged.json()["acknowledged_by"] == "operator"
    assert after_ack.json()["alerts"] == []
    acknowledged_pass = next(
        value for value in history.json()["passes"] if value["id"] == alert_pass_id
    )
    assert acknowledged_pass["acknowledged_at"]
    assert acknowledged_pass["acknowledged_by"] == "operator"


def test_hive_http_serves_the_page_and_event_script(client: TestClient) -> None:
    page = client.get("/")
    script = client.get("/web/hive.js")

    assert page.status_code == 200
    assert 'id="hive-map"' in page.text
    assert script.status_code == 200
    assert "/api/hive/events" in script.text
