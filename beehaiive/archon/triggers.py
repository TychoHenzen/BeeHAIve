"""Archon trigger bindings on disk, and the Windows tasks that fire them.

A binding is a JSON file that tells `archon trigger fire --config <file>` which
workflow to start, where, and as whom. Archon installs schedules for bindings
only on macOS, so on Windows a Task Scheduler task runs that command on a timer.
"""

from __future__ import annotations

import copy
import csv
import json
import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from ..records import Record, as_dict, as_list, dig, get_str
from .cli import ArchonCliError, Runner, run_process
from .settings import read_json_object

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}")
INPUT_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
OVERLAPS = ("skip", "queue")
# Archon 0.14's timer schema requires launch.isolation and accepts these kinds.
ISOLATIONS = ("worktree", "default", "in-place")
TASK_PREFIX = "Archon-"
SCRIPT = Path(__file__).resolve().parent / "schedule.ps1"
# archon.exe (the CLI and each `trigger execute`) and archon-server-*.exe; a
# run's metadata.execution_owner names the one that runs it.
ARCHON_IMAGES = "IMAGENAME eq archon*"


class BindingError(ValueError):
    """A binding the person has to fix. `message` is written for them."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def check_name(name: str) -> str:
    if not NAME.fullmatch(name):
        raise BindingError(
            "A binding name uses letters, digits, dots, dashes and underscores."
        )
    return name


@dataclass(frozen=True)
class NewBinding:
    """The fields a person fills in to create a binding file."""

    name: str
    workflow: str
    cwd: str
    resource: str
    host_id: str
    run_as_user_id: str
    interval_seconds: int
    inputs: Mapping[str, str] = field(default_factory=dict[str, str])
    overlap: str = "skip"
    capacity: int = 1
    isolation: str = "worktree"
    run_at_load: bool = False

    def validate(self) -> None:
        check_name(self.name)
        if not NAME.fullmatch(self.workflow):
            raise BindingError("Pick a workflow.")
        if not Path(self.cwd).is_dir():
            raise BindingError(f"{self.cwd} is not a folder on this machine.")
        if not self.resource.strip():
            raise BindingError("A binding needs a resource name.")
        if not NAME.fullmatch(self.host_id):
            raise BindingError("A host id uses letters, digits, dots and dashes.")
        if not self.run_as_user_id.strip():
            raise BindingError("A binding runs as an Archon user id.")
        if self.interval_seconds < 60:
            raise BindingError("Fire at most once a minute.")
        if self.overlap not in OVERLAPS:
            raise BindingError("Overlap is skip or queue.")
        if not 1 <= self.capacity <= 32:
            raise BindingError("Capacity is between 1 and 32.")
        if self.isolation not in ISOLATIONS:
            raise BindingError("Isolation is worktree, default or in-place.")
        for key in self.inputs:
            if not INPUT_KEY.fullmatch(key):
                raise BindingError(f"{key!r} is not a workflow input name.")

    def document(self) -> Record:
        launch: Record = {
            "cwd": self.cwd,
            "workflowName": self.workflow,
            "inputs": dict(self.inputs),
            "isolation": {"kind": self.isolation},
        }
        return {
            "version": 1,
            "sourceInstanceId": self.name,
            "binding": {
                "bindingId": self.name,
                "bindingRevision": None,
                "hostId": self.host_id,
                "runAsUserId": self.run_as_user_id,
                "resource": self.resource.strip(),
                "capacity": self.capacity,
                "overlap": self.overlap,
                "launch": launch,
            },
            "schedule": {
                "intervalSeconds": self.interval_seconds,
                "runAtLoad": self.run_at_load,
            },
        }

    def apply_to(self, document: Record) -> Record:
        """An existing binding file with this binding's editable fields set.

        The ids, the run-as user, runAtLoad and every key this form does not
        know stay as the file has them, so an edit never drops what Archon or
        a person put there.
        """
        edited = copy.deepcopy(document)
        binding = as_dict(edited.get("binding"))
        if binding is None:
            raise BindingError("This file is not a binding, so it cannot be edited.")
        binding.update(
            hostId=self.host_id,
            resource=self.resource.strip(),
            capacity=self.capacity,
            overlap=self.overlap,
        )
        launch: Record = as_dict(binding.get("launch")) or {}
        launch.update(
            cwd=self.cwd, workflowName=self.workflow, inputs=dict(self.inputs)
        )
        # Archon checks each isolation kind's own keys, so a new kind starts bare.
        isolation = as_dict(launch.get("isolation"))
        if isolation is None or isolation.get("kind") != self.isolation:
            launch["isolation"] = {"kind": self.isolation}
        binding["launch"] = launch
        schedule: Record = as_dict(edited.get("schedule")) or {"runAtLoad": False}
        schedule["intervalSeconds"] = self.interval_seconds
        edited["schedule"] = schedule
        return edited


class TriggerStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def path_for(self, name: str) -> Path:
        return self.directory / f"{check_name(name)}.json"

    def existing(self, name: str) -> Path:
        path = self.path_for(name)
        if not path.is_file():
            raise KeyError(name)
        return path

    def summaries(self) -> list[Record]:
        if not self.directory.is_dir():
            return []
        paths = sorted(self.directory.glob("*.json"), key=lambda p: p.name.lower())
        return [summarize(path) for path in paths if NAME.fullmatch(path.stem)]

    def summary(self, name: str) -> Record:
        return summarize(self.existing(name))

    def delete(self, name: str) -> None:
        self.existing(name).unlink()

    def create(self, binding: NewBinding) -> Path:
        binding.validate()
        path = self.path_for(binding.name)
        self.directory.mkdir(parents=True, exist_ok=True)
        text = json.dumps(binding.document(), indent=2) + "\n"
        try:
            with path.open("x", encoding="utf-8") as handle:
                handle.write(text)
        except FileExistsError as error:
            raise BindingError(
                f"A binding named {binding.name} already exists."
            ) from error
        return path

    def update(self, binding: NewBinding) -> Path:
        binding.validate()
        path = self.existing(binding.name)
        document = read_json_object(path)
        if document is None:
            raise BindingError(f"{path.name} is not JSON, so it cannot be edited.")
        text = json.dumps(binding.apply_to(document), indent=2) + "\n"
        # A scheduled task can fire while this writes, so it must never see half
        # a file: write beside it, then swap. The temp name does not end in
        # .json, so a listing never shows it.
        temp = path.with_name(f".{path.name}.edit")
        temp.write_text(text, encoding="utf-8")
        os.replace(temp, path)
        return path


def summarize(path: Path) -> Record:
    document = read_json_object(path)
    binding = as_dict(dig(document, "binding"))
    launch = as_dict(dig(binding, "launch"))
    interval = dig(document, "schedule", "intervalSeconds")
    capacity = dig(binding, "capacity")
    return {
        "name": path.stem,
        "path": str(path),
        "error": None if binding is not None else "This file is not a binding.",
        "bindingId": get_str(binding, "bindingId"),
        "hostId": get_str(binding, "hostId"),
        "runAsUserId": get_str(binding, "runAsUserId"),
        "resource": get_str(binding, "resource"),
        "overlap": get_str(binding, "overlap"),
        "capacity": capacity if isinstance(capacity, int) else None,
        "workflow": get_str(launch, "workflowName"),
        "cwd": get_str(launch, "cwd"),
        "inputs": as_dict(dig(launch, "inputs")) or {},
        "isolation": get_str(as_dict(dig(launch, "isolation")), "kind"),
        "intervalSeconds": interval if isinstance(interval, int) else None,
    }


def task_name(binding_name: str) -> str:
    return TASK_PREFIX + check_name(binding_name)


class Scheduler:
    """The Windows side of trigger bindings: the Task Scheduler entries named
    Archon-<binding>, and which archon processes are alive to run what they fire.

    A fire starts a separate `archon trigger execute` process for its run and
    exits, so a task is back to Ready while its run goes on."""

    def __init__(
        self,
        binary: Path,
        runner: Runner = run_process,
        platform: str = sys.platform,
        shell: str = "powershell.exe",
    ) -> None:
        self.binary = binary
        self._runner = runner
        self.supported = platform == "win32"
        self._shell = shell

    def _script(self, *args: str) -> str:
        if not self.supported:
            raise ArchonCliError("Scheduling from BeeHAIve works only on Windows.")
        command = [
            self._shell,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(SCRIPT),
            *args,
        ]
        result = self._runner(command, None, 60.0)
        if not result.ok:
            raise ArchonCliError("Task Scheduler refused the change.", result.output)
        return result.stdout

    def list(self) -> list[Record]:
        try:
            loaded: object = json.loads(self._script("-Action", "List") or "[]")
        except ValueError as error:
            raise ArchonCliError("Task Scheduler did not answer in JSON.") from error
        rows = [as_dict(row) for row in as_list(loaded)]
        return [row for row in rows if row is not None]

    def install(self, binding_name: str, config: Path, cwd: str, interval: int) -> None:
        self._script(
            "-Action",
            "Install",
            "-Name",
            task_name(binding_name),
            "-Exe",
            str(self.binary),
            "-Config",
            str(config),
            "-WorkingDirectory",
            cwd,
            "-IntervalSeconds",
            str(interval),
        )

    def remove(self, binding_name: str) -> None:
        self._script("-Action", "Remove", "-Name", task_name(binding_name))

    def pause(self) -> int:
        """Disable every Archon- task, so no binding fires on its own until
        resume(). Runs already going keep going. Returns how many changed."""
        return _task_count(self._script("-Action", "Pause"))

    def resume(self) -> int:
        """Enable every disabled Archon- task. Returns how many changed."""
        return _task_count(self._script("-Action", "Resume"))

    def archon_processes(self) -> list[int]:
        """The process ids of every archon program alive on this machine."""
        if not self.supported:
            raise ArchonCliError("Checking processes works only on Windows.")
        command = ["tasklist.exe", "/FI", ARCHON_IMAGES, "/FO", "CSV", "/NH"]
        result = self._runner(command, None, 30.0)
        if not result.ok:
            raise ArchonCliError("tasklist did not list processes.", result.output)
        # No match prints one INFO line, which has no id column.
        rows = csv.reader(result.stdout.splitlines())
        return [int(row[1]) for row in rows if len(row) > 1 and row[1].isdigit()]


def _task_count(text: str) -> int:
    count = text.strip()
    if not count.isdigit():
        raise ArchonCliError("Task Scheduler did not say how many tasks changed.")
    return int(count)
