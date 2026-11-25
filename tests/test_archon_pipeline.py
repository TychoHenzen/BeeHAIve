from __future__ import annotations

import json
import threading
from collections.abc import Sequence
from pathlib import Path

import pytest

from beehaiive.archon.cli import CliResult
from beehaiive.archon.pipeline import LEDGER, PipelineReader, pipelines
from tests.archon_fakes import ACTION, ArchonHarness, archon_harness, printed

LEDGER_READ = {"repository": "o/repo", "counts": {"stuck": 2}}


def binding(home: Path, name: str, resource: str, cwd: Path) -> None:
    triggers = home / "triggers"
    triggers.mkdir(exist_ok=True)
    document = {
        "binding": {
            "bindingId": name,
            "resource": resource,
            "launch": {"cwd": str(cwd), "workflowName": "pbi-build-queue"},
        },
        "schedule": {"intervalSeconds": 1800},
    }
    (triggers / f"{name}.json").write_text(json.dumps(document), "utf-8")


def served(tmp_path: Path, clock: list[float] | None = None) -> ArchonHarness:
    harness = archon_harness(
        tmp_path, clock=(lambda: clock[0]) if clock is not None else None
    )
    binding(harness.home, "o-build", "pbi:github.com/o/repo:build", tmp_path)
    binding(harness.home, "o-refine", "pbi:github.com/o/repo:refine", tmp_path)
    binding(harness.home, "cleanup", "cleanup:github.com/o/repo", tmp_path)
    script = harness.home.joinpath(*LEDGER)
    script.parent.mkdir(parents=True)
    script.write_text("// ledger", "utf-8")
    harness.pipeline_runner.answers["ledger.mjs read"] = printed(LEDGER_READ)
    return harness


def test_pipelines_are_the_repositories_pbi_bindings_serve(tmp_path: Path) -> None:
    found = pipelines(
        [
            {"resource": "pbi:github.com/o/repo:build", "cwd": "C:/repo"},
            {"resource": "pbi:github.com/o/repo:merge", "cwd": "C:/other"},
            {"resource": "pbi:github.com/o/--version:build", "cwd": "C:/x"},
            {"resource": "forge:github.com/o/b:build", "cwd": "C:/b"},
            {"resource": "pbi:github.com/o/c:build", "cwd": ""},
            {"resource": None, "cwd": "C:/d"},
        ]
    )

    # A name that could read as an option never matches.
    assert found == {"o/repo": Path("C:/repo")}


def test_the_page_lists_served_repositories_without_reading(tmp_path: Path) -> None:
    harness = served(tmp_path)

    answer = harness.client.get("/api/archon/pipeline").json()

    assert answer == {"repositories": ["o/repo"], "ledger": None}
    assert harness.pipeline_runner.calls == []


def test_a_read_runs_the_ledger_in_the_queues_folder(tmp_path: Path) -> None:
    harness = served(tmp_path)

    read = harness.client.post(
        "/api/archon/pipeline", json={"repository": "o/repo"}, headers=ACTION
    )

    assert read.json() == {"ledger": LEDGER_READ}
    script = str(harness.home.joinpath(*LEDGER))
    assert harness.pipeline_runner.calls == [
        ["node", script, "read", "--repository=o/repo"]
    ]
    assert harness.pipeline_runner.cwds == [tmp_path]


def test_a_reading_is_kept_for_five_minutes(tmp_path: Path) -> None:
    clock = [100.0]
    harness = served(tmp_path, clock)
    harness.client.post(
        "/api/archon/pipeline", json={"repository": "o/repo"}, headers=ACTION
    )

    clock[0] = 100.0 + 299
    kept = harness.client.get("/api/archon/pipeline?repository=o/repo").json()
    clock[0] = 100.0 + 300
    expired = harness.client.get("/api/archon/pipeline?repository=o/repo").json()

    assert kept["ledger"] == LEDGER_READ
    assert expired["ledger"] is None
    assert len(harness.pipeline_runner.calls) == 1


def test_a_repository_no_binding_serves_is_refused(tmp_path: Path) -> None:
    harness = served(tmp_path)

    read = harness.client.post(
        "/api/archon/pipeline", json={"repository": "o/other"}, headers=ACTION
    )
    listed = harness.client.get("/api/archon/pipeline?repository=--help")

    assert read.status_code == 404
    assert listed.status_code == 404
    assert harness.pipeline_runner.calls == []


def test_a_read_must_come_from_the_page(tmp_path: Path) -> None:
    harness = served(tmp_path)

    read = harness.client.post("/api/archon/pipeline", json={"repository": "o/repo"})

    assert read.status_code == 403
    assert harness.pipeline_runner.calls == []


def test_a_ledger_that_fails_is_a_502_with_its_output(tmp_path: Path) -> None:
    harness = served(tmp_path)
    harness.pipeline_runner.answers["ledger.mjs read"] = CliResult(
        2, "", "ledger.mjs: gh could not read the project"
    )

    read = harness.client.post(
        "/api/archon/pipeline", json={"repository": "o/repo"}, headers=ACTION
    )

    assert read.status_code == 502
    assert read.json() == {
        "error": "The pipeline could not be read.",
        "detail": "ledger.mjs: gh could not read the project",
    }


def test_an_archon_home_without_the_ledger_says_so(tmp_path: Path) -> None:
    harness = served(tmp_path)
    harness.home.joinpath(*LEDGER).unlink()

    read = harness.client.post(
        "/api/archon/pipeline", json={"repository": "o/repo"}, headers=ACTION
    )

    assert read.status_code == 502
    assert "has no ledger.mjs" in read.json()["error"]
    assert harness.pipeline_runner.calls == []


def test_a_read_that_waited_takes_the_reading_it_waited_for(tmp_path: Path) -> None:
    script = tmp_path.joinpath(*LEDGER)
    script.parent.mkdir(parents=True)
    script.write_text("// ledger", "utf-8")
    release, second_asked = threading.Event(), threading.Event()
    ticks = [0.0]
    calls: list[list[str]] = []

    def clock() -> float:
        ticks[0] += 1
        if ticks[0] == 2:
            second_asked.set()
        return ticks[0]

    def runner(args: Sequence[str], _cwd: Path | None, _timeout: float) -> CliResult:
        calls.append(list(args))
        release.wait(5)
        return printed(LEDGER_READ)

    reader = PipelineReader(tmp_path, runner=runner, node="node", clock=clock)
    results: list[object] = []
    first = threading.Thread(
        target=lambda: results.append(reader.read("o/r", tmp_path))
    )
    second = threading.Thread(
        target=lambda: results.append(reader.read("o/r", tmp_path))
    )
    first.start()
    while not calls:
        threading.Event().wait(0.01)
    second.start()
    assert second_asked.wait(5)
    release.set()
    first.join(5)
    second.join(5)

    assert results == [LEDGER_READ, LEDGER_READ]
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("stdout", "error"),
    [
        ("not json", "ledger.mjs did not print JSON."),
        ("[1, 2]", "ledger.mjs did not print a JSON object."),
    ],
)
def test_a_ledger_that_prints_no_object_is_a_502(
    tmp_path: Path, stdout: str, error: str
) -> None:
    harness = served(tmp_path)
    harness.pipeline_runner.answers["ledger.mjs read"] = CliResult(0, stdout, "")

    read = harness.client.post(
        "/api/archon/pipeline", json={"repository": "o/repo"}, headers=ACTION
    )

    assert read.status_code == 502
    assert read.json() == {"error": error, "detail": stdout}
