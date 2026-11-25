"""Saving a binding against the capacity Archon holds, and how each binding's
last fire went. Archon fixes a resource's capacity at its first fire, and
`archon trigger list` says "matched" for a fire it then refused."""

from __future__ import annotations

import json
from pathlib import Path

from beehaiive.archon.cli import ArchonCli, CliResult
from beehaiive.archon.receipts import MAX_INSPECTS, ReceiptReader, lookback
from beehaiive.records import Record, dig
from tests.archon_fakes import ACTION, ArchonHarness, FakeRunner, archon_harness, printed

NAME = "repo-pbi-build-queue"
RESOURCE = "pbi:github.com/o/repo:build"
BINDINGS = "/api/archon/triggers/bindings"


def binding_form(cwd: Path, **changes: object) -> dict[str, object]:
    base: dict[str, object] = {
        "name": NAME,
        "workflow": "pbi-build-queue",
        "cwd": str(cwd),
        "resource": RESOURCE,
        "hostId": "local",
        "runAsUserId": "u1",
        "intervalSeconds": 1800,
        "capacity": 1,
    }
    return base | changes


def edit_form(cwd: Path, **changes: object) -> dict[str, object]:
    base = binding_form(cwd, **changes)
    for fixed in ("name", "runAsUserId"):
        base.pop(fixed)
    return base


def fire(
    number: int,
    capacity: int,
    *,
    source: str = NAME,
    status: str = "complete",
    request: str | None = "admitted",
    error: str | None = None,
    resource: str = RESOURCE,
) -> tuple[Record, Record]:
    """A `trigger list` row and its `trigger inspect` document. Fires are
    numbered from the newest, a minute apart."""
    receipt_id = f"rcpt-{number:04d}"
    minute = 59 - number % 60
    row: Record = {
        "id": receipt_id,
        "sourceInstanceId": source,
        "outcome": "matched",
        "receivedAt": f"2026-11-25T{18 - number // 60:02d}:{minute:02d}:00.000Z",
    }
    entry: Record = {
        "bindingId": source,
        "status": status,
        "error": error,
        "intent": {"resource": resource, "capacity": capacity, "overlap": "skip"},
        "requestStatus": request,
    }
    return row, {"id": receipt_id, "bindings": [entry]}


def refused(number: int, capacity: int, *, source: str = NAME) -> tuple[Record, Record]:
    return fire(
        number,
        capacity,
        source=source,
        status="rejected",
        request=None,
        error="resource_capacity_conflict",
    )


def script(runner: FakeRunner, fires: list[tuple[Record, Record]]) -> None:
    runner.answers["trigger list"] = printed([row for row, _ in fires])
    for row, document in fires:
        runner.answers[f"trigger inspect {row['id']}"] = printed(document)


def inspected(runner: FakeRunner) -> list[str]:
    return [call[-1] for call in runner.calls if "inspect" in call]


def saved_capacity(harness: ArchonHarness) -> object:
    path = harness.home / "triggers" / f"{NAME}.json"
    return dig(json.loads(path.read_text("utf-8")), "binding", "capacity")


def with_build_binding(tmp_path: Path, capacity: int = 1) -> ArchonHarness:
    harness = archon_harness(tmp_path)
    script(harness.runner, [])
    made = harness.client.post(
        BINDINGS, json=binding_form(tmp_path, capacity=capacity), headers=ACTION
    )
    assert made.status_code == 200 and made.json()["warning"] is None
    harness.runner.calls.clear()
    return harness


def test_raising_the_capacity_of_a_resource_archon_holds_is_refused(
    tmp_path: Path,
) -> None:
    # What happened to the CardCleaner build queue on 2026-11-25: capacity 1
    # became 3, and Archon refused every fire after it.
    harness = with_build_binding(tmp_path)
    script(harness.runner, [fire(0, 1, request="skipped"), fire(1, 1)])

    answer = harness.client.put(
        f"{BINDINGS}/{NAME}", json=edit_form(tmp_path, capacity=3), headers=ACTION
    )

    assert answer.status_code == 400
    error = answer.json()["error"]
    assert f"fixed the capacity of {RESOURCE} at 1" in error
    assert "Keep capacity 1, or give this binding a new resource name." in error
    assert saved_capacity(harness) == 1
    assert inspected(harness.runner) == ["rcpt-0000"]


def test_the_held_capacity_is_found_past_a_long_run_of_refused_fires(
    tmp_path: Path,
) -> None:
    # The fix for a stuck binding: two dozen refused fires, then accepted ones.
    harness = with_build_binding(tmp_path, capacity=3)
    fires = [refused(n, 3) for n in range(24)] + [fire(n, 1) for n in range(24, 30)]
    script(harness.runner, fires)

    back = harness.client.put(
        f"{BINDINGS}/{NAME}", json=edit_form(tmp_path, capacity=1), headers=ACTION
    )
    again = harness.client.put(
        f"{BINDINGS}/{NAME}", json=edit_form(tmp_path, capacity=3), headers=ACTION
    )

    assert back.status_code == 200 and back.json()["warning"] is None
    assert saved_capacity(harness) == 1
    assert again.status_code == 400
    # The newest, then doubling distances back; the second save reads nothing new.
    assert inspected(harness.runner) == [
        "rcpt-0000",
        "rcpt-0001",
        "rcpt-0003",
        "rcpt-0007",
        "rcpt-0015",
        "rcpt-0029",
    ]


def test_a_capacity_no_fire_has_used_saves_without_a_word(tmp_path: Path) -> None:
    harness = archon_harness(tmp_path)
    script(harness.runner, [fire(0, 2, source="other-binding", resource="pbi:x:y")])

    made = harness.client.post(BINDINGS, json=binding_form(tmp_path), headers=ACTION)

    assert made.status_code == 200
    assert made.json()["warning"] is None
    assert inspected(harness.runner) == []


def test_an_edit_that_keeps_resource_and_capacity_asks_archon_nothing(
    tmp_path: Path,
) -> None:
    harness = with_build_binding(tmp_path)

    edited = harness.client.put(
        f"{BINDINGS}/{NAME}",
        json=edit_form(tmp_path, overlap="queue"),
        headers=ACTION,
    )

    assert edited.status_code == 200 and edited.json()["warning"] is None
    assert harness.runner.calls == []


def test_a_save_archon_cannot_be_asked_about_goes_ahead_with_a_warning(
    tmp_path: Path,
) -> None:
    harness = with_build_binding(tmp_path)
    harness.runner.answers["trigger list"] = CliResult(1, "", "database is locked")

    edited = harness.client.put(
        f"{BINDINGS}/{NAME}", json=edit_form(tmp_path, capacity=2), headers=ACTION
    ).json()

    assert edited["binding"]["capacity"] == 2
    assert "could not ask Archon" in edited["warning"]
    assert "database is locked" in edited["warning"]


def test_a_listing_archon_marks_failed_is_not_read_as_no_fires(tmp_path: Path) -> None:
    harness = with_build_binding(tmp_path)
    harness.runner.answers["trigger list"] = printed({"ok": False, "error": "no db"})

    edited = harness.client.put(
        f"{BINDINGS}/{NAME}", json=edit_form(tmp_path, capacity=2), headers=ACTION
    ).json()

    assert "Archon refused the command: no db" in edited["warning"]


def test_fires_that_prove_nothing_leave_a_warning(tmp_path: Path) -> None:
    harness = with_build_binding(tmp_path)
    script(harness.runner, [refused(0, 1), fire(1, 1, status="preparing", request=None)])

    edited = harness.client.put(
        f"{BINDINGS}/{NAME}", json=edit_form(tmp_path, capacity=2), headers=ACTION
    ).json()

    assert edited["binding"]["capacity"] == 2
    assert f"No fire of {RESOURCE} that BeeHAIve read shows" in edited["warning"]


def test_a_fire_on_another_resource_proves_nothing_about_this_one(
    tmp_path: Path,
) -> None:
    # The binding used another resource before; those fires say nothing here.
    harness = with_build_binding(tmp_path)
    script(harness.runner, [fire(0, 3, resource="pbi:github.com/o/repo:old")])

    edited = harness.client.put(
        f"{BINDINGS}/{NAME}", json=edit_form(tmp_path, capacity=3), headers=ACTION
    ).json()

    assert edited["binding"]["capacity"] == 3
    assert "unchecked" in edited["warning"]


def test_a_new_binding_must_match_what_archon_holds_for_a_shared_resource(
    tmp_path: Path,
) -> None:
    harness = with_build_binding(tmp_path)
    script(harness.runner, [fire(0, 1)])

    other = binding_form(tmp_path, name="repo-pbi-build-queue-2", capacity=2)
    answer = harness.client.post(BINDINGS, json=other, headers=ACTION)

    assert answer.status_code == 400
    assert "at 1" in answer.json()["error"]
    assert not (harness.home / "triggers" / "repo-pbi-build-queue-2.json").exists()


def test_bindings_that_disagree_before_any_fire_get_a_warning(tmp_path: Path) -> None:
    harness = with_build_binding(tmp_path)

    other = binding_form(tmp_path, name="repo-pbi-build-queue-2", capacity=2)
    made = harness.client.post(BINDINGS, json=other, headers=ACTION).json()

    assert made["binding"]["capacity"] == 2
    assert f"{NAME} also uses {RESOURCE}, with capacity 1" in made["warning"]


def test_a_bad_binding_is_refused_before_archon_is_asked(tmp_path: Path) -> None:
    harness = archon_harness(tmp_path)

    answer = harness.client.post(
        BINDINGS, json=binding_form(tmp_path, capacity=0), headers=ACTION
    )

    assert answer.status_code == 400
    assert "Capacity" in answer.json()["error"]
    assert harness.runner.calls == []


def test_health_says_which_bindings_archon_refuses_and_why(tmp_path: Path) -> None:
    harness = with_build_binding(tmp_path, capacity=3)
    for name in ("repo-pbi-merge-queue", "repo-pbi-refine-queue", "repo-cleanup"):
        made = harness.client.post(
            BINDINGS,
            json=binding_form(tmp_path, name=name, resource=f"pbi:{name}"),
            headers=ACTION,
        )
        assert made.status_code == 200
    fires = [
        refused(0, 3),
        fire(1, 1, source="repo-pbi-merge-queue", resource="pbi:repo-pbi-merge-queue", request="skipped"),
        refused(2, 3),
        fire(3, 1),
    ]
    script(harness.runner, fires)
    harness.runner.answers["trigger inspect rcpt-0099"] = CliResult(1, "", "gone")
    row, _ = fire(99, 1, source="repo-pbi-refine-queue")
    listed = [r for r, _ in fires] + [row]
    harness.runner.answers["trigger list"] = printed(listed)

    health = harness.client.get("/api/archon/triggers/health").json()["bindings"]

    stuck = health[NAME]
    assert stuck["state"] == "stuck"
    assert stuck["error"] == "resource_capacity_conflict"
    assert stuck["receiptId"] == "rcpt-0000"
    assert stuck["detail"] == (
        f"Archon holds {RESOURCE} at capacity 1, and this binding declares 3. "
        "Set its capacity to 1, or give it a new resource name."
    )
    merge = health["repo-pbi-merge-queue"]
    assert merge["state"] == "ok"
    assert merge["detail"] == "Archon skipped it: the resource was full."
    assert health["repo-pbi-refine-queue"]["state"] == "unknown"
    assert "gone" in health["repo-pbi-refine-queue"]["detail"]
    assert health["repo-cleanup"] == {"state": "quiet", "detail": "No recent fire."}


def test_health_names_other_refusals_in_plain_words(tmp_path: Path) -> None:
    harness = with_build_binding(tmp_path)
    row, document = fire(0, 1, status="rejected", request=None, error="run_as_user_failed")
    script(harness.runner, [(row, document)])

    health = harness.client.get("/api/archon/triggers/health").json()["bindings"]

    assert health[NAME]["state"] == "stuck"
    assert health[NAME]["detail"] == "The Archon user this binding runs as no longer exists."


def test_health_is_a_502_when_archon_cannot_list_its_fires(tmp_path: Path) -> None:
    harness = with_build_binding(tmp_path)
    harness.runner.answers["trigger list"] = CliResult(1, "", "no database")

    answer = harness.client.get("/api/archon/triggers/health")

    assert answer.status_code == 502
    assert answer.json()["detail"] == "no database"


def test_lookback_doubles_back_and_ends_at_the_oldest() -> None:
    assert lookback(0) == []
    assert lookback(1) == [0]
    assert lookback(5) == [0, 1, 3, 4]
    many = lookback(5000)
    assert many[:4] == [0, 1, 3, 7] and many[-1] == 4999
    assert len(many) == MAX_INSPECTS


def test_a_receipt_id_that_could_read_as_an_option_is_never_inspected() -> None:
    runner = FakeRunner()
    reader = ReceiptReader(ArchonCli(Path("archon"), runner=runner))

    assert reader.fire({"id": "--help", "sourceInstanceId": NAME}, NAME) is None
    assert runner.calls == []


def test_fires_dropped_from_archons_listing_are_forgotten() -> None:
    runner = FakeRunner()
    reader = ReceiptReader(ArchonCli(Path("archon"), runner=runner))
    script(runner, [fire(0, 1)])
    reader.fire(reader.recent()[0], NAME)
    script(runner, [fire(1, 1)])
    del runner.answers["trigger inspect rcpt-0000"]
    runner.answers["trigger inspect rcpt-0000"] = printed({"bindings": []})

    reader.recent()

    assert reader.fire({"id": "rcpt-0000", "receivedAt": ""}, NAME) is None


def test_health_without_proof_still_says_why_archon_refuses(tmp_path: Path) -> None:
    # The older fire's receipt cannot be read, so nothing proves the number.
    harness = with_build_binding(tmp_path, capacity=3)
    script(harness.runner, [refused(0, 3), refused(1, 3), fire(2, 1)])
    harness.runner.answers["trigger inspect rcpt-0001"] = CliResult(1, "", "gone")

    health = harness.client.get("/api/archon/triggers/health").json()["bindings"]

    assert health[NAME]["state"] == "stuck"
    assert health[NAME]["detail"] == (
        f"Archon holds {RESOURCE} at a capacity other than the 3 this binding "
        "declares. Every binding for one resource must declare the capacity the "
        "resource first ran with."
    )


def test_health_says_a_fire_archon_still_prepares_is_pending(tmp_path: Path) -> None:
    harness = with_build_binding(tmp_path)
    script(harness.runner, [fire(0, 1, status="preparing", request=None)])

    health = harness.client.get("/api/archon/triggers/health").json()["bindings"]

    assert health[NAME]["state"] == "pending"
    assert health[NAME]["detail"] == "Archon is still preparing it (preparing)."
