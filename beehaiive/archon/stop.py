"""Stop a trigger binding: withdraw the starts it queued, and say what still runs.

Removing a binding's Windows task stops its own fires, but not what Archon
already queued for it. A binding with overlap "queue" leaves one start request
per fire while its resource is full, and `archon trigger fire` from any other
binding drains the whole host, so those requests keep starting runs after the
trigger looks off. On 2026-11-26 four CardCleaner refine runs started that way
after the refine trigger was unscheduled.

Archon has no command that lists queued requests, so they are found through
the binding's receipts: `trigger list` names each fire's binding, and `trigger
inspect` says what became of the start it asked for. A run a binding started
keeps going until it ends or is cancelled, and one parked at the provider's
usage limit continues by itself when the limit resets, so its live runs are
listed for the person to decide on.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from ..records import Record, as_dict, as_list, dig, get_str
from .cli import ArchonCli, ArchonCliError

# A receipt or request id becomes a CLI argument, so it may not read as an option.
ARCHON_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_:.-]{0,127}")
LIST_LIMIT = 1000
# ASSUMPTION: sixty receipts (about fifteen seconds at four at a time) is the
# longest a click should wait. A binding reaches a started run well before
# that unless every fire for half a day was refused or skipped.
MAX_INSPECTS = 60
INSPECT_WORKERS = 4


@dataclass(frozen=True)
class Fire:
    """What one receipt says about the start this binding asked for."""

    receipt_id: str
    received_at: str
    resource: str
    # admitted, queued, skipped or withdrawn; "" when the fire asked for no
    # start, as when Archon refused it.
    request: str
    request_id: str

    @property
    def queued(self) -> bool:
        return self.request == "queued"


@dataclass
class Search:
    queued: list[Fire] = field(default_factory=list[Fire])
    unread: int = 0
    # Whether every fire that could still hold a queued start was read.
    complete: bool = True
    inspected: int = 0


def _rows(cli: ArchonCli) -> list[Record]:
    loaded = cli.ask(["trigger", "list", "--limit", str(LIST_LIMIT)])
    rows = [row for row in map(as_dict, as_list(loaded)) if row is not None]
    if not rows and loaded != []:
        raise ArchonCliError("archon trigger list did not print a list of receipts.")
    return rows


def _fire(cli: ArchonCli, row: Record, binding_id: str) -> Fire | None:
    """What `row`'s receipt says about this binding, or None when it cannot be read."""
    receipt_id = get_str(row, "id")
    if receipt_id is None or not ARCHON_ID.fullmatch(receipt_id):
        return None
    try:
        loaded = as_dict(cli.ask(["trigger", "inspect", receipt_id]))
    except ArchonCliError:
        return None
    entries = [as_dict(entry) for entry in as_list(dig(loaded, "bindings"))]
    entry = next(
        (e for e in entries if e is not None and get_str(e, "bindingId") == binding_id),
        None,
    )
    disposition = as_dict(dig(entry, "disposition"))
    request = get_str(entry, "requestStatus") or ""
    # Archon gives the request id only while the request is not withdrawn, and
    # a queued one must say so twice before it is withdrawn.
    request_id = get_str(disposition, "requestId") or ""
    if get_str(disposition, "status") != request:
        request_id = ""
    return Fire(
        receipt_id=receipt_id,
        received_at=get_str(row, "receivedAt") or "",
        resource=get_str(as_dict(dig(entry, "intent")), "resource") or "",
        request=request,
        request_id=request_id,
    )


def find_queued(cli: ArchonCli, source_id: str, binding_id: str) -> Search:
    """The starts this binding asked for that Archon still holds in its queue.

    Archon admits a resource's queued requests in the order they came, and a
    new request is admitted at once only when nothing is queued ahead of it.
    So a start this binding got admitted proves that none of its older starts
    on that resource is still queued, and the walk back through its fires
    stops there. A skipped fire proves nothing: once a binding goes from queue
    back to skip, its newer fires are skipped behind the older queued ones.

    ASSUMPTION: the binding kept the same launch (folder and isolation) while
    it had starts queued. Archon passes over a queued start whose checkout is
    busy, so a start with another launch could be admitted ahead of it.
    """
    rows = _rows(cli)
    mine = [row for row in rows if get_str(row, "sourceInstanceId") == source_id]
    mine.sort(key=lambda row: get_str(row, "receivedAt") or "", reverse=True)
    search = Search()
    used: set[str] = set()
    proven: set[str] = set()
    stopped = False

    def read(row: Record) -> Fire | None:
        return _fire(cli, row, binding_id)

    with ThreadPoolExecutor(INSPECT_WORKERS) as pool:
        for start in range(0, min(len(mine), MAX_INSPECTS), INSPECT_WORKERS):
            chunk = mine[start : min(start + INSPECT_WORKERS, MAX_INSPECTS)]
            search.inspected += len(chunk)
            for fire in pool.map(read, chunk):
                if fire is None:
                    search.unread += 1
                    continue
                if not fire.request:
                    continue
                used.add(fire.resource)
                if fire.request == "admitted":
                    proven.add(fire.resource)
                if fire.queued:
                    search.queued.append(fire)
            if used and used <= proven:
                stopped = True
                break
    # Without a started run to stop at, every fire must have been read, and
    # the listing must hold all of Archon's receipts, not just the newest.
    walked_all = search.inspected == len(mine) and len(rows) < LIST_LIMIT
    search.complete = stopped or walked_all
    return search


def _request_status(cli: ArchonCli, request_id: str) -> str | None:
    try:
        return get_str(as_dict(cli.ask(["trigger", "inspect", request_id])), "status")
    except ArchonCliError:
        return None


def withdraw(cli: ArchonCli, fire: Fire) -> tuple[str, str]:
    """Withdraw one queued start: ("withdrawn", ""), ("moved", its status now)
    when it left the queue first, or ("failed", why)."""
    if not ARCHON_ID.fullmatch(fire.request_id):
        return "failed", "Archon did not give this queued start a request id."
    try:
        cli.ask(["trigger", "withdraw", fire.request_id])
    except ArchonCliError as error:
        # Another binding's fire drains the host and may admit it first;
        # Archon then refuses, since only untouched queued work can go.
        status = _request_status(cli, fire.request_id)
        if status is not None and status != "queued":
            return "moved", status
        return "failed", error.message
    return "withdrawn", ""


def live_runs(cli: ArchonCli, binding_id: str, cwd: Path | None) -> list[Record]:
    """The binding's runs that are running or paused.

    `workflow status` lists only those two, and it needs a git checkout as
    its folder even with --all, so it runs in the binding's own folder.
    A run Archon admitted seconds ago is still pending and not listed yet.
    """
    loaded = cli.ask(["workflow", "status", "--all"], cwd)
    runs: list[Record] = []
    for run in map(as_dict, as_list(dig(loaded, "runs"))):
        metadata = as_dict(dig(run, "metadata"))
        if get_str(as_dict(dig(metadata, "resource_start")), "bindingId") != binding_id:
            continue
        runs.append(
            {
                "id": get_str(run, "id"),
                "workflow": get_str(run, "workflow_name"),
                "status": get_str(run, "status"),
                # "quota" when Archon parked it at the usage limit, "wait"
                # when it waits for an answer.
                "pauseCause": get_str(metadata, "pause_cause"),
                "startedAt": get_str(run, "started_at"),
            }
        )
    return runs


def _start(fire: Fire, **extra: object) -> Record:
    return {
        "requestId": fire.request_id,
        "receiptId": fire.receipt_id,
        "receivedAt": fire.received_at,
        **extra,
    }


def _search_warnings(search: Search) -> Sequence[str]:
    warnings: list[str] = []
    if search.unread:
        warnings.append(
            f"{search.unread} of this binding's fires could not be read from "
            "Archon, so a start one of them queued may still be waiting."
        )
    if not search.complete and search.inspected:
        warnings.append(
            f"BeeHAIve read this binding's {search.inspected} newest fires and "
            "none of them started a run, so it stopped there. A start queued "
            "before them may still be waiting."
        )
    elif not search.complete:
        warnings.append(
            f"None of Archon's newest {LIST_LIMIT} receipts is from this binding, "
            "so a start it queued before them was not looked for."
        )
    return warnings


def stop_binding(
    cli: ArchonCli, source_id: str, binding_id: str, cwd: Path | None
) -> Record:
    """Withdraw every start this binding still has queued, then list its live runs.

    `error` is set when Archon's receipts could not be listed at all, and
    `failed` holds the starts that are still queued. Either means the binding
    is not stopped yet; `stopped` says whether it is.
    """
    withdrawn: list[Record] = []
    moved: list[Record] = []
    failed: list[Record] = []
    warnings: list[str] = []
    error: str | None = None
    try:
        search = find_queued(cli, source_id, binding_id)
    except ArchonCliError as problem:
        error = (
            "BeeHAIve could not read this binding's fires from Archon, so it "
            f"withdrew nothing: {problem.message}"
        )
    else:
        warnings.extend(_search_warnings(search))
        for fire in search.queued:
            outcome, said = withdraw(cli, fire)
            if outcome == "withdrawn":
                withdrawn.append(_start(fire))
            elif outcome == "moved":
                moved.append(_start(fire, status=said))
            else:
                failed.append(_start(fire, error=said))
    # Listed after withdrawing, so a start admitted in the meantime shows here.
    # None, not an empty list, when Archon could not say.
    live: list[Record] | None
    try:
        live = live_runs(cli, binding_id, cwd)
    except ArchonCliError as problem:
        live = None
        warnings.append(
            f"BeeHAIve could not list this binding's live runs: {problem.message}"
        )
    return {
        "stopped": error is None and not failed,
        "withdrawn": withdrawn,
        "moved": moved,
        "failed": failed,
        "live": live,
        "error": error,
        "warnings": warnings,
    }
