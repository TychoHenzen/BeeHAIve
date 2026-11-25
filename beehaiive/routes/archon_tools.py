"""What Archon's HTTP API lacks, done through its CLI and files.

Triggers and schedules exist only in the CLI, so the console's Triggers page
reads and writes binding files and runs fixed `archon trigger` commands here.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import cast

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..archon.cli import ArchonCli, ArchonCliError
from ..archon.settings import ArchonSettings
from ..archon.triggers import (
    BindingError,
    NewBinding,
    Scheduler,
    TriggerStore,
    task_name,
)

router = APIRouter(prefix="/api/archon")

RECEIPT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_:.-]{0,127}")
FIRE_TIMEOUT = 300.0


class BindingChanges(BaseModel):
    """What an edit may change. The name, ids and run-as user stay fixed: the
    name is also the file, the Windows task and what queue inputs point at."""

    workflow: str
    cwd: str
    resource: str
    host_id: str = Field(alias="hostId")
    interval_seconds: int = Field(alias="intervalSeconds")
    inputs: dict[str, str] = Field(default_factory=dict[str, str])
    overlap: str = "skip"
    capacity: int = 1
    isolation: str = "worktree"

    def binding(
        self, name: str, run_as_user_id: str, run_at_load: bool = False
    ) -> NewBinding:
        return NewBinding(
            name=name.strip(),
            workflow=self.workflow.strip(),
            cwd=self.cwd.strip(),
            resource=self.resource,
            host_id=self.host_id.strip(),
            run_as_user_id=run_as_user_id.strip(),
            interval_seconds=self.interval_seconds,
            inputs=self.inputs,
            overlap=self.overlap,
            capacity=self.capacity,
            isolation=self.isolation,
            run_at_load=run_at_load,
        )


class BindingForm(BindingChanges):
    name: str
    run_as_user_id: str = Field(alias="runAsUserId")
    run_at_load: bool = Field(default=False, alias="runAtLoad")

    def new_binding(self) -> NewBinding:
        return self.binding(self.name, self.run_as_user_id, self.run_at_load)


def _settings(request: Request) -> ArchonSettings:
    return cast(ArchonSettings, request.app.state.archon_settings)


def _cli(request: Request) -> ArchonCli:
    return cast(ArchonCli, request.app.state.archon_cli)


def _store(request: Request) -> TriggerStore:
    return cast(TriggerStore, request.app.state.trigger_store)


def _scheduler(request: Request) -> Scheduler:
    return cast(Scheduler, request.app.state.scheduler)


def _failed(error: Exception, status: int = 502) -> JSONResponse:
    detail = error.detail if isinstance(error, ArchonCliError) else ""
    message = str(error) if not isinstance(error, KeyError) else "No such binding."
    return JSONResponse({"error": message, "detail": detail}, status_code=status)


@router.get("/settings")
def settings(request: Request) -> dict[str, object]:
    archon = _settings(request)
    return {
        "url": archon.url,
        "home": str(archon.home),
        "binary": str(archon.binary),
        "defaultHome": archon.is_default_home,
    }


@router.get("/identity", response_model=None)
def identity(request: Request) -> dict[str, object] | JSONResponse:
    try:
        return {"identity": _cli(request).json(["trigger", "whoami"])}
    except ArchonCliError as error:
        return _failed(error)


@router.get("/triggers/bindings")
def bindings(request: Request) -> dict[str, object]:
    settings = _settings(request)
    return {
        "directory": str(_store(request).directory),
        "binary": str(settings.binary),
        "defaultHome": settings.is_default_home,
        "bindings": _store(request).summaries(),
    }


@router.post("/triggers/bindings", response_model=None)
def create_binding(
    form: BindingForm, request: Request
) -> dict[str, object] | JSONResponse:
    try:
        path = _store(request).create(form.new_binding())
    except BindingError as error:
        return _failed(error, 400)
    return {"binding": _store(request).summary(path.stem)}


@router.put("/triggers/bindings/{name}", response_model=None)
def update_binding(
    name: str, changes: BindingChanges, request: Request
) -> dict[str, object] | JSONResponse:
    store, scheduler = _store(request), _scheduler(request)
    try:
        before = store.summary(name)
    except (BindingError, KeyError) as error:
        return _failed(error, 404)
    run_as = before["runAsUserId"]
    if before["error"] is not None or not isinstance(run_as, str):
        return JSONResponse(
            {"error": "This file is not a binding, so it cannot be edited."},
            status_code=400,
        )
    try:
        path = store.update(changes.binding(name, run_as))
    except BindingError as error:
        return _failed(error, 400)
    after = store.summary(name)
    # The Windows task keeps the interval and folder it was installed with, so
    # a change to either reinstalls it. Every other field is read from the file
    # each time the task fires.
    interval, cwd = after["intervalSeconds"], after["cwd"]
    moved = (interval, cwd) != (before["intervalSeconds"], before["cwd"])
    rescheduled = False
    if moved and scheduler.supported and isinstance(interval, int):
        try:
            scheduled = any(
                task.get("name") == task_name(name) for task in scheduler.list()
            )
            if scheduled and isinstance(cwd, str):
                scheduler.install(name, path, cwd, interval)
                rescheduled = True
        except ArchonCliError as error:
            return JSONResponse(
                {
                    "error": "The binding is saved, but its Windows task still "
                    "runs on the old schedule. Unschedule and schedule it again.",
                    "detail": error.detail or str(error),
                },
                status_code=502,
            )
    return {"binding": after, "rescheduled": rescheduled}


@router.delete("/triggers/bindings/{name}", response_model=None)
def delete_binding(name: str, request: Request) -> dict[str, object] | JSONResponse:
    # A Windows task left behind would keep firing a file that is gone, so the
    # task goes first, and a task that will not go keeps the file as well.
    # Archon's own history of past fires stays either way.
    store, scheduler = _store(request), _scheduler(request)
    try:
        store.existing(name)
        scheduled = scheduler.supported and any(
            task.get("name") == task_name(name) for task in scheduler.list()
        )
        if scheduled:
            scheduler.remove(name)
        store.delete(name)
    except (BindingError, KeyError) as error:
        return _failed(error, 404)
    except ArchonCliError as error:
        return _failed(error)
    except OSError as error:
        return _failed(error, 500)
    return {"deleted": True, "unscheduled": scheduled}


@router.post("/triggers/bindings/{name}/fire", response_model=None)
def fire(name: str, request: Request) -> dict[str, object] | JSONResponse:
    try:
        path = _store(request).existing(name)
        # Run where the scheduled task runs: the binding's own folder.
        cwd = _store(request).summary(name)["cwd"]
        folder = Path(cwd) if isinstance(cwd, str) and Path(cwd).is_dir() else None
        result = _cli(request).run(
            ["trigger", "fire", "--config", str(path)],
            cwd=folder,
            timeout=FIRE_TIMEOUT,
        )
    except (BindingError, KeyError) as error:
        return _failed(error, 404)
    except ArchonCliError as error:
        return _failed(error)
    return {"ok": result.ok, "code": result.code, "output": result.output}


@router.get("/triggers/receipts", response_model=None)
def receipts(request: Request, limit: int = 50) -> dict[str, object] | JSONResponse:
    limit = max(1, min(limit, 1000))
    try:
        return {
            "receipts": _cli(request).json(["trigger", "list", "--limit", str(limit)])
        }
    except ArchonCliError as error:
        return _failed(error)


@router.get("/triggers/receipts/{receipt_id}", response_model=None)
def receipt(receipt_id: str, request: Request) -> dict[str, object] | JSONResponse:
    # The id becomes a CLI argument, so it may not look like an option.
    if not RECEIPT_ID.fullmatch(receipt_id):
        return JSONResponse({"error": "That is not a receipt id."}, status_code=400)
    try:
        result = _cli(request).run(["trigger", "inspect", receipt_id])
    except ArchonCliError as error:
        return _failed(error)
    try:
        return {"ok": result.ok, "receipt": json.loads(result.stdout)}
    except ValueError:
        return {"ok": result.ok, "text": result.output}


@router.get("/triggers/schedules", response_model=None)
def schedules(request: Request) -> dict[str, object] | JSONResponse:
    scheduler = _scheduler(request)
    if not scheduler.supported:
        return {"supported": False, "tasks": []}
    try:
        return {"supported": True, "tasks": scheduler.list()}
    except ArchonCliError as error:
        return _failed(error)


@router.put("/triggers/bindings/{name}/schedule", response_model=None)
def install_schedule(name: str, request: Request) -> dict[str, object] | JSONResponse:
    try:
        summary = _store(request).summary(name)
    except (BindingError, KeyError) as error:
        return _failed(error, 404)
    interval, cwd = summary["intervalSeconds"], summary["cwd"]
    if not isinstance(interval, int) or not isinstance(cwd, str):
        return JSONResponse(
            {"error": "The binding needs launch.cwd and schedule.intervalSeconds."},
            status_code=400,
        )
    try:
        _scheduler(request).install(name, _store(request).existing(name), cwd, interval)
    except ArchonCliError as error:
        return _failed(error)
    return {"installed": True}


@router.delete("/triggers/bindings/{name}/schedule", response_model=None)
def remove_schedule(name: str, request: Request) -> dict[str, object] | JSONResponse:
    try:
        _scheduler(request).remove(name)
    except BindingError as error:
        return _failed(error, 404)
    except ArchonCliError as error:
        return _failed(error)
    return {"removed": True}
