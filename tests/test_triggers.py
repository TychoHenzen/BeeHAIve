from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from beehaiive.archon.cli import ArchonCliError, CliResult
from beehaiive.archon.triggers import (
    SCRIPT,
    BindingError,
    NewBinding,
    Scheduler,
    TriggerStore,
    task_name,
)
from beehaiive.records import as_dict, dig
from tests.archon_fakes import FakeRunner, printed


def binding(folder: Path, **changes: Any) -> NewBinding:
    base = NewBinding(
        name="beehaiive-build-queue",
        workflow="archon-build-queue",
        cwd=str(folder),
        resource="forge:github.com/o/r:build",
        host_id="local",
        run_as_user_id="u1",
        interval_seconds=1800,
        inputs={"repo_host": "github.com"},
        overlap="skip",
        capacity=2,
    )
    return replace(base, **changes)


def test_a_binding_document_has_the_shape_archon_reads(tmp_path: Path) -> None:
    document = binding(tmp_path).document()

    assert document["version"] == 1
    assert document["sourceInstanceId"] == "beehaiive-build-queue"
    assert document["schedule"] == {"intervalSeconds": 1800, "runAtLoad": False}
    inner = document["binding"]
    assert isinstance(inner, dict)
    assert inner["bindingId"] == "beehaiive-build-queue"
    assert inner["launch"] == {
        "cwd": str(tmp_path),
        "workflowName": "archon-build-queue",
        "inputs": {"repo_host": "github.com"},
        "isolation": {"kind": "worktree"},
    }


def test_isolation_is_always_written(tmp_path: Path) -> None:
    # Archon 0.14 refuses a timer binding without launch.isolation.
    inner = binding(tmp_path, isolation="in-place").document()["binding"]

    assert isinstance(inner, dict)
    assert inner["launch"]["isolation"] == {"kind": "in-place"}


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"name": "../evil"}, "binding name"),
        ({"workflow": "-x"}, "Pick a workflow"),
        ({"cwd": "Z:/does/not/exist"}, "not a folder"),
        ({"resource": " "}, "resource"),
        ({"host_id": "a b"}, "host id"),
        ({"run_as_user_id": ""}, "user id"),
        ({"interval_seconds": 30}, "once a minute"),
        ({"overlap": "drop"}, "Overlap"),
        ({"capacity": 0}, "Capacity"),
        ({"isolation": "vm"}, "Isolation"),
        ({"inputs": {"bad key": "x"}}, "input name"),
    ],
)
def test_validate_refuses_each_bad_field(
    tmp_path: Path, changes: dict[str, object], message: str
) -> None:
    with pytest.raises(BindingError, match=message):
        binding(tmp_path, **changes).validate()


def test_store_creates_lists_and_summarizes_bindings(tmp_path: Path) -> None:
    store = TriggerStore(tmp_path / "triggers")
    assert store.summaries() == []

    path = store.create(binding(tmp_path))
    (tmp_path / "triggers" / "notes.json").write_text("[]", "utf-8")
    (tmp_path / "triggers" / "bad name!.json").write_text("{}", "utf-8")

    assert path == tmp_path / "triggers" / "beehaiive-build-queue.json"
    assert json.loads(path.read_text("utf-8"))["binding"]["capacity"] == 2
    names = [s["name"] for s in store.summaries()]
    assert names == ["beehaiive-build-queue", "notes"]
    summary = store.summary("beehaiive-build-queue")
    assert summary["workflow"] == "archon-build-queue"
    assert summary["intervalSeconds"] == 1800
    assert summary["isolation"] == "worktree"
    assert summary["error"] is None
    assert store.summary("notes")["error"] == "This file is not a binding."


def test_store_never_overwrites_a_binding(tmp_path: Path) -> None:
    store = TriggerStore(tmp_path / "triggers")
    store.create(binding(tmp_path))

    with pytest.raises(BindingError, match="already exists"):
        store.create(binding(tmp_path))


def test_store_refuses_unknown_and_unsafe_names(tmp_path: Path) -> None:
    store = TriggerStore(tmp_path)

    with pytest.raises(KeyError):
        store.existing("missing")
    with pytest.raises(BindingError):
        store.path_for("..\\up")


def test_task_names_carry_the_archon_prefix() -> None:
    assert task_name("x-build-queue") == "Archon-x-build-queue"


def test_scheduler_lists_tasks_through_the_script() -> None:
    rows = [{"name": "Archon-a", "state": "Ready"}, "junk"]
    runner = FakeRunner({"-Action List": printed(rows)})
    scheduler = Scheduler(Path("archon.exe"), runner=runner, platform="win32")

    assert scheduler.list() == [{"name": "Archon-a", "state": "Ready"}]
    command = runner.calls[0]
    assert command[:6] == [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
    ]
    assert command[6] == str(SCRIPT)


def test_scheduler_install_and_remove_pass_fixed_arguments(tmp_path: Path) -> None:
    runner = FakeRunner(
        {
            "-Action Install": CliResult(0, "installed", ""),
            "-Action Remove": CliResult(0, "removed", ""),
        }
    )
    scheduler = Scheduler(Path("archon.exe"), runner=runner, platform="win32")

    scheduler.install("a", tmp_path / "a.json", str(tmp_path), 1800)
    scheduler.remove("a")

    install, remove = runner.calls
    assert install[7:] == [
        "-Action",
        "Install",
        "-Name",
        "Archon-a",
        "-Exe",
        "archon.exe",
        "-Config",
        str(tmp_path / "a.json"),
        "-WorkingDirectory",
        str(tmp_path),
        "-IntervalSeconds",
        "1800",
    ]
    assert remove[7:] == ["-Action", "Remove", "-Name", "Archon-a"]


def test_scheduler_reports_a_refusal_and_bad_output() -> None:
    refused = Scheduler(
        Path("a"),
        runner=FakeRunner({"Remove": CliResult(1, "", "denied")}),
        platform="win32",
    )
    garbled = Scheduler(
        Path("a"),
        runner=FakeRunner({"List": CliResult(0, "<xml>", "")}),
        platform="win32",
    )

    with pytest.raises(ArchonCliError, match="refused") as caught:
        refused.remove("a")
    assert caught.value.detail == "denied"
    with pytest.raises(ArchonCliError, match="JSON"):
        garbled.list()


def test_scheduler_is_windows_only() -> None:
    runner = FakeRunner()
    scheduler = Scheduler(Path("a"), runner=runner, platform="linux")

    assert scheduler.supported is False
    with pytest.raises(ArchonCliError, match="only on Windows"):
        scheduler.list()
    assert runner.calls == []


def test_an_edit_resets_isolation_only_when_its_kind_changes(tmp_path: Path) -> None:
    document = binding(tmp_path).document()
    launch = as_dict(dig(document, "binding", "launch"))
    assert launch is not None
    launch["isolation"] = {"kind": "worktree", "branch": "keep/me"}

    same = binding(tmp_path, capacity=5).apply_to(document)
    other = binding(tmp_path, isolation="in-place").apply_to(document)

    isolation = ("binding", "launch", "isolation")
    assert dig(same, *isolation) == {"kind": "worktree", "branch": "keep/me"}
    assert dig(other, *isolation) == {"kind": "in-place"}
    # The original document is never changed in place.
    assert dig(document, *isolation) == {"kind": "worktree", "branch": "keep/me"}
    assert dig(document, "binding", "capacity") == 2


def test_store_update_validates_and_refuses_what_is_not_a_binding(
    tmp_path: Path,
) -> None:
    store = TriggerStore(tmp_path / "triggers")
    store.create(binding(tmp_path))
    (tmp_path / "triggers" / "notes.json").write_text("[]", "utf-8")

    with pytest.raises(BindingError, match="Capacity"):
        store.update(binding(tmp_path, capacity=99))
    with pytest.raises(KeyError):
        store.update(binding(tmp_path, name="missing"))
    with pytest.raises(BindingError, match="not JSON"):
        store.update(binding(tmp_path, name="notes"))

    store.update(binding(tmp_path, capacity=3))
    assert store.summary("beehaiive-build-queue")["capacity"] == 3
