"""What Archon decided about each trigger fire, read from its receipts.

`archon trigger fire` exits 0 and `archon trigger list` says "matched" even
when Archon refuses to start the workflow, so only `archon trigger inspect`
shows that a binding is stuck. Archon also fixes a resource's capacity the
first time a fire uses it and refuses every later fire that declares another
number. No command shows or changes that number, and a fire Archon accepted is
the only proof of it, so the console reads it from receipts before it saves a
binding.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Collection, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from ..records import Record, as_dict, as_list, get_str
from .cli import ArchonCli, ArchonCliError
from .triggers import NewBinding

RECEIPT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_:.-]{0,127}")
LIST_LIMIT = 1000
# Archon checks a fire's capacity against the resource's when it takes a slot,
# so a fire it admitted, skipped or queued declared the number Archon holds.
ACCEPTED = ("admitted", "skipped", "queued")
FINAL = ("complete", "rejected")
# ASSUMPTION: ten inspects (about nine seconds) is the longest a save should
# wait for proof. They sit at doubling distances into a binding's receipts, so
# a long run of refused fires (two dozen on the CardCleaner build queue on
# 2026-11-25) does not hide the accepted ones before it.
MAX_INSPECTS = 10
INSPECT_WORKERS = 4

PLAIN_ERRORS = {
    "run_as_user_failed": "The Archon user this binding runs as no longer exists.",
    "run_configuration_failed": "Archon could not read this binding's run configuration.",
    "launch_preparation_failed": "Archon could not prepare the workflow launch.",
}
STARTED = {
    "admitted": "Archon started its workflow.",
    "skipped": "Archon skipped it: the resource was full.",
    "queued": "Archon queued it for a free slot.",
}


@dataclass(frozen=True)
class Fire:
    """One binding's part of a receipt: what it declared and what Archon did."""

    receipt_id: str
    received_at: str
    status: str
    error: str
    request_status: str
    resource: str
    capacity: int | None

    def proves(self, resource: str) -> int | None:
        """The capacity Archon holds for `resource`, if this fire proves it."""
        accepted = self.status == "complete" and self.request_status in ACCEPTED
        return self.capacity if accepted and self.resource == resource else None


@dataclass(frozen=True)
class Held:
    """The capacity Archon holds for a resource, when a receipt proves it, and
    how many receipts of the bindings that use it there were to look in."""

    capacity: int | None
    receipts: int


@dataclass(frozen=True)
class Verdict:
    refusal: str | None = None
    warning: str | None = None


def _checked(value: object) -> object:
    # Archon's JSON mode can report a failure as {"ok": false} with exit 0.
    loaded = as_dict(value)
    if loaded is not None and loaded.get("ok") is False:
        said = get_str(loaded, "error") or "ok: false"
        raise ArchonCliError(f"Archon refused the command: {said}")
    return value


def lookback(count: int) -> list[int]:
    """Which of `count` receipts, newest first, to inspect: the newest, then
    doubling distances back, then the oldest."""
    picked: list[int] = []
    step = 1
    while step - 1 < count and len(picked) < MAX_INSPECTS - 1:
        picked.append(step - 1)
        step *= 2
    if count and count - 1 not in picked:
        picked.append(count - 1)
    return picked


class ReceiptReader:
    def __init__(self, cli: ArchonCli) -> None:
        self._cli = cli
        # A fire that is complete or rejected never changes, and neither does a
        # capacity Archon holds, so both are kept.
        # ASSUMPTION: nobody deletes a slot from Archon's database while
        # BeeHAIve runs; Archon itself has no command that does.
        self._fires: dict[tuple[str, str], Fire] = {}
        self._held: dict[str, int] = {}
        self._guard = threading.Lock()

    def recent(self) -> list[Record]:
        """Archon's latest receipts, newest first."""
        loaded = _checked(
            self._cli.json(["trigger", "list", "--limit", str(LIST_LIMIT)])
        )
        rows = [row for row in map(as_dict, as_list(loaded)) if row is not None]
        rows.sort(key=lambda row: get_str(row, "receivedAt") or "", reverse=True)
        listed = {get_str(row, "id") for row in rows}
        with self._guard:
            for key in [key for key in self._fires if key[0] not in listed]:
                del self._fires[key]
        return rows

    def fire(self, receipt: Record, binding: str) -> Fire | None:
        """What `binding` declared in this receipt and what Archon did with it."""
        receipt_id = get_str(receipt, "id")
        if receipt_id is None or not RECEIPT_ID.fullmatch(receipt_id):
            return None
        with self._guard:
            kept = self._fires.get((receipt_id, binding))
        if kept is not None:
            return kept
        loaded = as_dict(_checked(self._cli.json(["trigger", "inspect", receipt_id])))
        rows = [as_dict(row) for row in as_list((loaded or {}).get("bindings"))]
        # ASSUMPTION: a binding's id is its file name, as the console writes it.
        entry = next(
            (row for row in rows if row and get_str(row, "bindingId") == binding), None
        )
        if entry is None:
            return None
        intent = as_dict(entry.get("intent"))
        capacity = (intent or {}).get("capacity")
        found = Fire(
            receipt_id=receipt_id,
            received_at=get_str(receipt, "receivedAt") or "",
            status=get_str(entry, "status") or "",
            error=get_str(entry, "error") or "",
            request_status=get_str(entry, "requestStatus") or "",
            resource=get_str(intent, "resource") or "",
            capacity=capacity if type(capacity) is int else None,
        )
        if found.status in FINAL:
            with self._guard:
                self._fires[(receipt_id, binding)] = found
        return found

    def held(
        self, resource: str, bindings: Collection[str], receipts: Sequence[Record]
    ) -> Held:
        """The capacity Archon holds for `resource`, from the receipts of the
        named bindings.

        ASSUMPTION: a resource is used only by the bindings that name it now
        and by the one being saved. A deleted binding that used it under
        another name is not looked at.
        """
        mine = [r for r in receipts if get_str(r, "sourceInstanceId") in bindings]
        with self._guard:
            kept = self._held.get(resource)
        if kept is not None:
            return Held(kept, len(mine))
        for index in lookback(len(mine)):
            receipt = mine[index]
            found = self.fire(receipt, get_str(receipt, "sourceInstanceId") or "")
            proven = found.proves(resource) if found else None
            if proven is not None:
                with self._guard:
                    self._held[resource] = proven
                return Held(proven, len(mine))
        return Held(None, len(mine))


def _users(resource: str, others: Sequence[Record]) -> list[Record]:
    return [other for other in others if other.get("resource") == resource]


def check_capacity(
    reader: ReceiptReader, binding: NewBinding, others: Sequence[Record]
) -> Verdict:
    """Whether Archon will take the capacity `binding` declares.

    `others` are the summaries of every other binding file. A capacity Archon
    is proven to hold refuses any other number. Without proof the save goes
    ahead with a warning, because a silent pass is how a refused binding went
    unnoticed for hours.
    """
    resource = binding.resource.strip()
    sharing = _users(resource, others)
    names = {binding.name, *(str(other.get("name")) for other in sharing)}
    warnings: list[str] = []
    try:
        held = reader.held(resource, names, reader.recent())
    except ArchonCliError as error:
        held = Held(None, 0)
        warnings.append(
            f"BeeHAIve could not ask Archon which capacity it holds for {resource} "
            f"({error.message}), so this capacity is unchecked."
        )
    if held.capacity is not None:
        if held.capacity == binding.capacity:
            return Verdict()
        return Verdict(
            refusal=f"Archon fixed the capacity of {resource} at {held.capacity} "
            "the first time it ran, and it refuses every fire that declares "
            f"another number. Keep capacity {held.capacity}, or give this "
            "binding a new resource name."
        )
    if held.receipts:
        warnings.append(
            f"No fire of {resource} that BeeHAIve read shows which capacity Archon "
            "holds, so this capacity is unchecked. If Archon refuses the next "
            "fire, this page says why."
        )
    for other in sharing:
        if other.get("capacity") != binding.capacity:
            warnings.append(
                f"{other.get('name')} also uses {resource}, with capacity "
                f"{other.get('capacity')}. Archon refuses every fire whose "
                "capacity differs from the first one the resource ran with, so "
                "give both the same capacity."
            )
    return Verdict(warning=" ".join(warnings) or None)


def _stuck_reason(
    reader: ReceiptReader,
    found: Fire,
    sharing: Collection[str],
    receipts: Sequence[Record],
) -> str:
    if found.error != "resource_capacity_conflict":
        return PLAIN_ERRORS.get(
            found.error, f"Archon refused it: {found.error or 'no reason given'}."
        )
    try:
        held = reader.held(found.resource, sharing, receipts).capacity
    except ArchonCliError:
        held = None
    declared = found.capacity if found.capacity is not None else "another number"
    if held is None:
        return (
            f"Archon holds {found.resource} at a capacity other than the {declared} "
            "this binding declares. Every binding for one resource must declare "
            "the capacity the resource first ran with."
        )
    return (
        f"Archon holds {found.resource} at capacity {held}, and this binding "
        f"declares {declared}. Set its capacity to {held}, or give it a new "
        "resource name."
    )


def _inspected(
    reader: ReceiptReader, receipt: Record | None, name: str
) -> Fire | ArchonCliError | None:
    if receipt is None:
        return None
    try:
        return reader.fire(receipt, name)
    except ArchonCliError as error:
        return error


def binding_health(
    reader: ReceiptReader, bindings: Sequence[Record]
) -> dict[str, Record]:
    """How each binding's newest fire went, by binding name.

    `state` is "stuck" when Archon refused it, "ok" when Archon took it,
    "pending" while Archon still prepares it, "quiet" when no fire is among
    Archon's latest receipts, and "unknown" when its receipt could not be read.
    """
    receipts = reader.recent()
    newest: dict[str, Record] = {}
    for receipt in receipts:
        newest.setdefault(get_str(receipt, "sourceInstanceId") or "", receipt)
    names = [str(b.get("name")) for b in bindings if b.get("error") is None]

    def newest_fire(name: str) -> Fire | ArchonCliError | None:
        return _inspected(reader, newest.get(name), name)

    with ThreadPoolExecutor(INSPECT_WORKERS) as pool:
        fires = list(pool.map(newest_fire, names))
    health: dict[str, Record] = {}
    for name, found in zip(names, fires, strict=True):
        if isinstance(found, ArchonCliError):
            health[name] = {
                "state": "unknown",
                "detail": f"Archon did not say how its last fire went: {found.message}",
            }
            continue
        if found is None:
            health[name] = {"state": "quiet", "detail": "No recent fire."}
            continue
        if found.status == "rejected":
            sharing = {name, *(str(b.get("name")) for b in _users(found.resource, bindings))}
            state, detail = "stuck", _stuck_reason(reader, found, sharing, receipts)
        elif found.status == "complete":
            state = "ok"
            detail = STARTED.get(found.request_status, "Archon took it.")
        else:
            state, detail = "pending", f"Archon is still preparing it ({found.status})."
        health[name] = {
            "state": state,
            "detail": detail,
            "error": found.error or None,
            "receiptId": found.receipt_id,
            "receivedAt": found.received_at,
        }
    return health
