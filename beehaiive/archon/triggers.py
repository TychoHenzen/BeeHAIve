"""Archon trigger bindings on disk, and the Windows tasks that fire them.

A binding is a JSON file that tells `archon trigger fire --config <file>` which
workflow to start, where, and as whom. Archon installs schedules for bindings
only on macOS, so on Windows a Task Scheduler task runs that command on a timer.
"""

from __future__ import annotations

import json
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


class BindingError(ValueError):
    pass


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
    """Windows Task Scheduler entries named Archon-<binding>."""

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
