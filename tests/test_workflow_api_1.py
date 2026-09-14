from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from beehaiive.workflow import (
    WorkflowRole,
)
from main import create_app
from tests.support.workflow.helpers import make_service

CONSTITUTION_PATH = Path(__file__).parents[1] / "constitution.json"


def test_workflow_api_requires_server_configured_operator(tmp_path: Path) -> None:
    service, store, _ = make_service(tmp_path)
    client = TestClient(
        create_app(
            workflow_service=service,
            api_key="test-key",
            workflow_actor=WorkflowRole.WRITER,
        )
    )

    response = client.post(
        "/workflow/handoffs/missing/approve",
        json={"note": "caller cannot choose the actor"},
        headers={"X-API-Key": "test-key"},
    )

    assert response.status_code == 403
    store.close()


def test_workflow_api_rejects_missing_or_invalid_operator_configuration(
    tmp_path: Path,
) -> None:
    for actor in (None, "not-a-role"):
        service_root = tmp_path / (actor or "missing")
        service_root.mkdir()
        service, store, _ = make_service(service_root)
        client = TestClient(
            create_app(
                workflow_service=service,
                api_key="test-key",
                workflow_actor=actor,
            )
        )
        response = client.post(
            "/workflow/handoffs/missing/approve",
            json={"note": "operator decision"},
            headers={"X-API-Key": "test-key"},
        )
        assert response.status_code == 503
        store.close()
