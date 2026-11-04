from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from beehaiive.app import create_app
from beehaiive.database import SnapshotDatabase
from beehaiive.snapshot import ProjectSnapshotService
from beehaiive.workflow_generator import Runner, WorkflowGenerationService
from tests.support import skills_config
from tests.workflow_support import (
    EmptyProvider,
    await_http_job,
    definition,
    write_skill,
)


@pytest.fixture
def database() -> Iterator[SnapshotDatabase]:
    database = SnapshotDatabase(":memory:")
    yield database
    database.close()


@pytest.fixture
def service(database: SnapshotDatabase) -> ProjectSnapshotService:
    return ProjectSnapshotService(EmptyProvider(), database, minimum_refresh_seconds=0)


def generation_app(
    tmp_path: Path,
    database: SnapshotDatabase,
    service: ProjectSnapshotService,
    runner: Runner,
) -> FastAPI:
    generator = WorkflowGenerationService(
        skills_config(tmp_path), service, runner=runner
    )
    return create_app(
        skills_config(tmp_path),
        database=database,
        service=service,
        workflow_generator=generator,
    )


def test_generation_retries_once_and_returns_failed_draft(
    tmp_path: Path, service: ProjectSnapshotService
) -> None:
    write_skill(tmp_path)
    calls: list[str] = []

    def runner(_command: list[str], prompt: str, result_path: Path) -> str:
        calls.append(prompt)
        result_path.write_text(
            json.dumps({"bad": True} if len(calls) == 1 else definition()),
            encoding="utf-8",
        )
        return ""

    generator = WorkflowGenerationService(
        skills_config(tmp_path), service, runner=runner
    )
    job = generator.start("Delivery", "Wait for a backlog item.", [])
    result: dict[str, Any] | None = None
    for _ in range(100):
        result = generator.get(job["id"])
        assert result is not None
        if result["status"] != "pending":
            break
        time.sleep(0.01)

    assert result is not None
    assert result["status"] == "succeeded"
    assert result["attempts"] == 2
    assert len(calls) == 2
    assert "Repair these validation errors" in calls[1]


def test_generation_reports_a_runner_failure_as_a_failed_job(
    tmp_path: Path, service: ProjectSnapshotService
) -> None:
    def broken_runner(*_args: object) -> str:
        raise RuntimeError("runner broke")

    failing = WorkflowGenerationService(
        skills_config(tmp_path),
        service,
        runner=broken_runner,
    )
    job = failing.start("Broken", "Prompt", [])
    result: dict[str, Any] | None = None
    for _ in range(100):
        result = failing.get(job["id"])
        assert result is not None
        if result["status"] != "pending":
            break
        time.sleep(0.01)
    assert result is not None
    assert result["status"] == "failed"
    assert result["error"] == "runner broke"


def test_generation_http_success_exposes_one_repair_and_codex_flags(
    tmp_path: Path, database: SnapshotDatabase, service: ProjectSnapshotService
) -> None:
    write_skill(tmp_path)
    calls: list[tuple[list[str], str]] = []

    def runner(command: list[str], prompt: str, result_path: Path) -> str:
        calls.append((command, prompt))
        result_path.write_text(json.dumps(definition()), encoding="utf-8")
        return ""

    app = generation_app(tmp_path, database, service, runner)
    with TestClient(app) as client:
        started = client.post(
            "/api/workflows/generate",
            json={"name": "Delivery", "prompt": "Run delivery.", "parameters": []},
        )
        assert started.status_code == 200
        result = await_http_job(client, started.json()["id"])

    assert result["status"] == "succeeded"
    assert result["attempts"] == 1
    assert result["draft"]["name"] == "Delivery"
    command = calls[0][0]
    assert "--sandbox" in command
    assert "read-only" in command
    assert "--ephemeral" in command
    assert "--skip-git-repo-check" in command
    assert "--output-schema" in command
    assert "-o" in command
    assert command[-1] == "-"


def test_generation_http_failure_after_one_repair_keeps_draft_and_errors(
    tmp_path: Path, database: SnapshotDatabase, service: ProjectSnapshotService
) -> None:
    calls: list[str] = []

    def runner(_command: list[str], prompt: str, result_path: Path) -> str:
        calls.append(prompt)
        result_path.write_text(json.dumps({"bad": True}), encoding="utf-8")
        return ""

    app = generation_app(tmp_path, database, service, runner)
    with TestClient(app) as client:
        started = client.post(
            "/api/workflows/generate",
            json={"name": "Broken", "prompt": "Run delivery.", "parameters": []},
        )
        assert started.status_code == 200
        result = await_http_job(client, started.json()["id"])

    assert result["status"] == "failed"
    assert result["attempts"] == 2
    assert result["draft"] is not None
    assert result["validation_errors"]
    assert len(calls) == 2
    assert "Repair these validation errors exactly once" in calls[1]
