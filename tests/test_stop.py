"""Unscheduling or deleting a binding stops it: its Windows task goes, and so
does every start Archon still holds queued for it. On 2026-11-26 the CardCleaner
refine binding was unscheduled with eight starts queued, and the merge queue's
fires kept starting them, one per fire."""

from __future__ import annotations

from pathlib import Path

from beehaiive.archon.cli import ArchonCli, ArchonCliError, CliResult
from beehaiive.archon.stop import LIST_LIMIT, MAX_INSPECTS, find_queued
from tests.archon_fakes import (
    ACTION,
    ArchonHarness,
    FakeRunner,
    TriggerQueue,
    archon_harness,
    printed,
    refused,
)

NAME = "repo-pbi-refine-queue"
OTHER = "repo-pbi-merge-queue"
BINDING = f"/api/archon/triggers/bindings/{NAME}"
SCHEDULE = f"{BINDING}/schedule"


def with_queue(
    tmp_path: Path, *, scheduled: bool = True
) -> tuple[ArchonHarness, TriggerQueue]:
    harness = archon_harness(tmp_path)
    made = harness.client.post(
        "/api/archon/triggers/bindings",
        json={
            "name": NAME,
            "workflow": "pbi-refine-queue",
            "cwd": str(tmp_path),
            "resource": "pbi:github.com/o/repo:refine",
            "hostId": "local",
            "runAsUserId": "u1",
            "intervalSeconds": 1800,
            "overlap": "queue",
        },
        headers=ACTION,
    )
    assert made.status_code == 200
    tasks = [{"name": f"Archon-{NAME}"}] if scheduled else []
    harness.scheduler_runner.answers["-Action List"] = printed(tasks)
    harness.scheduler_runner.answers["-Action Remove"] = CliResult(0, "removed", "")
    queue = TriggerQueue()
    harness.runner.handler = queue.answer
    return harness, queue


def live_run(binding: str, status: str, cause: str | None = None) -> dict[str, object]:
    metadata: dict[str, object] = {"resource_start": {"bindingId": binding}}
    if cause is not None:
        metadata["pause_cause"] = cause
    return {
        "id": f"run-{binding.removeprefix('repo-pbi-')}-{status}",
        "workflow_name": binding.removeprefix("repo-"),
        "status": status,
        "started_at": "2026-11-26T09:00:00Z",
        "metadata": metadata,
    }


def inspected(harness: ArchonHarness) -> list[str]:
    """The receipts inspected, sorted: a chunk's inspects run on parallel
    threads, so the order they reach the runner differs from run to run."""
    return sorted(
        call[3] for call in harness.runner.calls if call[1:3] == ["trigger", "inspect"]
    )


def withdraws(harness: ArchonHarness) -> list[str]:
    return [
        call[3] for call in harness.runner.calls if call[1:3] == ["trigger", "withdraw"]
    ]


def test_unscheduling_withdraws_every_start_the_binding_queued(tmp_path: Path) -> None:
    # The incident: a live refine run, and a queued start for every fire since.
    harness, queue = with_queue(tmp_path)
    for _ in range(4):
        queue.add(NAME, "queued")
    queue.add(OTHER, "queued", resource="pbi:github.com/o/repo:merge")
    queue.add(NAME, "admitted")
    queue.runs = [live_run(NAME, "paused", "quota"), live_run(OTHER, "running")]

    answer = harness.client.delete(SCHEDULE, headers=ACTION)

    assert answer.status_code == 200
    reply = answer.json()
    assert reply["removed"] is True
    assert "queued" not in queue.statuses(NAME)
    assert queue.statuses(OTHER) == ["queued"]
    stop = reply["stop"]
    assert stop["stopped"] is True
    assert [s["requestId"] for s in stop["withdrawn"]] == [
        "req-0000",
        "req-0001",
        "req-0002",
        "req-0003",
    ]
    assert stop["withdrawn"][0]["receiptId"] == "rcpt-0000"
    assert stop["live"] == [
        {
            "id": "run-refine-queue-paused",
            "workflow": "pbi-refine-queue",
            "status": "paused",
            "pauseCause": "quota",
            "startedAt": "2026-11-26T09:00:00Z",
        }
    ]
    assert stop["moved"] == stop["failed"] == stop["warnings"] == []
    assert stop["error"] is None
    task_calls = harness.scheduler_runner.calls
    assert "Remove" in task_calls[-1] and f"Archon-{NAME}" in task_calls[-1]


def test_starts_queued_behind_skipped_fires_are_found(tmp_path: Path) -> None:
    # The binding went back from queue to skip, so its newer fires were
    # skipped behind the starts it queued before.
    harness, queue = with_queue(tmp_path)
    for _ in range(5):
        queue.add(NAME, "skipped")
    queue.add(NAME, "withdrawn")
    queue.add(NAME, None)
    for _ in range(3):
        queue.add(NAME, "queued")
    queue.add(NAME, "admitted")

    stop = harness.client.delete(SCHEDULE, headers=ACTION).json()["stop"]

    assert "queued" not in queue.statuses(NAME)
    assert len(stop["withdrawn"]) == 3
    assert stop["warnings"] == []


def test_the_walk_stops_at_the_newest_started_run(tmp_path: Path) -> None:
    harness, queue = with_queue(tmp_path)
    queue.add(NAME, "queued")
    queue.add(NAME, "admitted")
    for _ in range(20):
        queue.add(NAME, "admitted")

    stop = harness.client.delete(SCHEDULE, headers=ACTION).json()["stop"]

    # Archon is asked four at a time, and the first four hold the proof.
    assert inspected(harness) == [f"rcpt-{n:04d}" for n in range(4)]
    assert [s["requestId"] for s in stop["withdrawn"]] == ["req-0000"]
    assert stop["warnings"] == []


def test_a_start_another_fire_drained_first_is_reported_not_failed(
    tmp_path: Path,
) -> None:
    harness, queue = with_queue(tmp_path)
    queue.add(NAME, "queued")
    queue.add(NAME, "queued")
    queue.add(NAME, "admitted")
    queue.drained.add("req-0001")

    stop = harness.client.delete(SCHEDULE, headers=ACTION).json()["stop"]

    assert stop["stopped"] is True
    assert [s["requestId"] for s in stop["withdrawn"]] == ["req-0000"]
    assert stop["moved"] == [
        {
            "requestId": "req-0001",
            "receiptId": "rcpt-0001",
            "receivedAt": queue.fires[1].received_at,
            "status": "admitted",
        }
    ]
    assert stop["failed"] == []


def test_a_start_that_will_not_go_is_reported_and_unscheduling_again_retries(
    tmp_path: Path,
) -> None:
    harness, queue = with_queue(tmp_path)
    queue.add(NAME, "queued")
    queue.add(NAME, "admitted")
    queue.stuck.add("req-0000")

    first = harness.client.delete(SCHEDULE, headers=ACTION).json()

    assert first["removed"] is True
    assert first["stop"]["stopped"] is False
    assert first["stop"]["failed"] == [
        {
            "requestId": "req-0000",
            "receiptId": "rcpt-0000",
            "receivedAt": queue.fires[0].received_at,
            "error": "Only untouched queued requests can be withdrawn.",
        }
    ]
    assert queue.statuses(NAME) == ["queued", "admitted"]

    # The task is gone now, so trying again only withdraws.
    queue.stuck.clear()
    harness.scheduler_runner.answers["-Action List"] = printed([])
    harness.scheduler_runner.calls.clear()
    second = harness.client.delete(SCHEDULE, headers=ACTION).json()

    assert second["removed"] is False
    assert second["stop"]["stopped"] is True
    assert queue.statuses(NAME) == ["withdrawn", "admitted"]
    assert not any("Remove" in call for call in harness.scheduler_runner.calls)


def test_a_task_that_will_not_go_withdraws_nothing(tmp_path: Path) -> None:
    harness, queue = with_queue(tmp_path)
    queue.add(NAME, "queued")
    harness.scheduler_runner.answers["-Action Remove"] = CliResult(
        1, "", "Access denied"
    )

    answer = harness.client.delete(SCHEDULE, headers=ACTION)

    assert answer.status_code == 502
    assert answer.json()["detail"] == "Access denied"
    assert queue.statuses(NAME) == ["queued"]
    assert harness.runner.calls == []


def test_unscheduling_says_when_archon_could_not_be_asked(tmp_path: Path) -> None:
    harness, _queue = with_queue(tmp_path)
    harness.runner.answers["trigger list"] = refused("database is locked")

    stop = harness.client.delete(SCHEDULE, headers=ACTION).json()["stop"]

    assert stop["stopped"] is False
    assert stop["error"] == (
        "BeeHAIve could not read this binding's fires from Archon, so it "
        "withdrew nothing: database is locked"
    )
    assert withdraws(harness) == []


def test_deleting_a_binding_withdraws_its_queued_starts_before_the_file_goes(
    tmp_path: Path,
) -> None:
    harness, queue = with_queue(tmp_path)
    queue.add(NAME, "queued")
    queue.add(NAME, "admitted")
    queue.runs = [live_run(NAME, "running")]

    answer = harness.client.delete(BINDING, headers=ACTION)

    assert answer.status_code == 200
    reply = answer.json()
    assert reply["deleted"] is True and reply["unscheduled"] is True
    assert [s["requestId"] for s in reply["stop"]["withdrawn"]] == ["req-0000"]
    assert [run["status"] for run in reply["stop"]["live"]] == ["running"]
    assert queue.statuses(NAME) == ["withdrawn", "admitted"]
    assert not (harness.home / "triggers" / f"{NAME}.json").exists()
    # The live runs are asked for in the binding's own folder, a git checkout.
    status_call = harness.runner.calls.index(
        next(
            call for call in harness.runner.calls if call[1:3] == ["workflow", "status"]
        )
    )
    assert harness.runner.cwds[status_call] == tmp_path


def test_a_delete_that_cannot_withdraw_keeps_the_file(tmp_path: Path) -> None:
    harness, queue = with_queue(tmp_path)
    queue.add(NAME, "queued")
    queue.add(NAME, "queued")
    queue.add(NAME, "admitted")
    queue.stuck.add("req-0001")

    answer = harness.client.delete(BINDING, headers=ACTION)

    assert answer.status_code == 502
    reply = answer.json()
    assert reply["error"] == (
        "1 queued start(s) of this binding could not be withdrawn. "
        "The binding file stays, so Delete can try again."
    )
    assert reply["detail"] == "Only untouched queued requests can be withdrawn."
    assert [s["requestId"] for s in reply["stop"]["withdrawn"]] == ["req-0000"]
    assert (harness.home / "triggers" / f"{NAME}.json").is_file()

    # Archon cannot be asked at all: the file stays as well.
    harness.runner.answers["trigger list"] = CliResult(1, "", "archon: no database")
    again = harness.client.delete(BINDING, headers=ACTION)

    assert again.status_code == 502
    assert again.json()["error"].startswith("BeeHAIve could not read this binding's")
    assert (harness.home / "triggers" / f"{NAME}.json").is_file()


def test_a_request_id_that_could_read_as_an_option_is_never_withdrawn(
    tmp_path: Path,
) -> None:
    harness, queue = with_queue(tmp_path)
    queue.add(NAME, "queued", request_id="--help")
    queue.add(NAME, "admitted")

    stop = harness.client.delete(SCHEDULE, headers=ACTION).json()["stop"]

    assert withdraws(harness) == []
    assert stop["failed"][0]["error"] == (
        "Archon did not give this queued start a request id."
    )


def test_live_runs_are_only_this_bindings_and_say_why_they_wait(
    tmp_path: Path,
) -> None:
    harness, queue = with_queue(tmp_path, scheduled=False)
    queue.runs = [
        live_run(NAME, "paused", "wait"),
        live_run(OTHER, "paused", "quota"),
        {"id": "hand-made", "status": "running", "metadata": {}},
    ]

    reply = harness.client.delete(SCHEDULE, headers=ACTION).json()

    assert reply["removed"] is False
    assert [(r["status"], r["pauseCause"]) for r in reply["stop"]["live"]] == [
        ("paused", "wait")
    ]


def test_live_runs_that_cannot_be_listed_leave_a_warning(tmp_path: Path) -> None:
    harness, _queue = with_queue(tmp_path)
    harness.runner.answers["workflow status"] = refused("Not in a git repository.")

    stop = harness.client.delete(SCHEDULE, headers=ACTION).json()["stop"]

    assert stop["stopped"] is True and stop["live"] is None
    assert stop["warnings"] == [
        "BeeHAIve could not list this binding's live runs: Not in a git repository."
    ]


def test_a_binding_never_started_is_walked_to_the_end(tmp_path: Path) -> None:
    harness, queue = with_queue(tmp_path)
    queue.add(NAME, "queued")
    queue.add(NAME, None)

    stop = harness.client.delete(SCHEDULE, headers=ACTION).json()["stop"]

    assert len(stop["withdrawn"]) == 1 and stop["warnings"] == []


def test_a_walk_cut_short_says_older_starts_may_wait(tmp_path: Path) -> None:
    harness, queue = with_queue(tmp_path)
    for _ in range(MAX_INSPECTS + 5):
        queue.add(NAME, "skipped")

    stop = harness.client.delete(SCHEDULE, headers=ACTION).json()["stop"]

    assert len(inspected(harness)) == MAX_INSPECTS
    assert stop["warnings"] == [
        f"BeeHAIve read this binding's {MAX_INSPECTS} newest fires and none of "
        "them started a run, so it stopped there. A start queued before them "
        "may still be waiting."
    ]


def queue_cli() -> tuple[ArchonCli, FakeRunner, TriggerQueue]:
    runner = FakeRunner()
    queue = TriggerQueue()
    runner.handler = queue.answer
    return ArchonCli(Path("archon"), runner=runner), runner, queue


def test_an_unreadable_receipt_is_counted_and_the_rest_still_read() -> None:
    cli, runner, queue = queue_cli()
    queue.add(NAME, "queued")
    queue.add(NAME, "skipped")
    queue.add(OTHER, "admitted")
    runner.answers["trigger inspect rcpt-0001"] = CliResult(1, "", "gone")

    search = find_queued(cli, NAME, NAME)

    assert [fire.request_id for fire in search.queued] == ["req-0000"]
    assert search.unread == 1 and search.complete is True
    assert "rcpt-0002" not in [call[3] for call in runner.calls if "inspect" in call]


def test_a_full_listing_may_hide_older_starts() -> None:
    # Archon lists at most LIST_LIMIT receipts, so with no started run to stop
    # at, older receipts of this binding may be missing from the listing.
    cli, runner, queue = queue_cli()
    queue.add(NAME, "skipped")
    others = [f"x-{n}" for n in range(LIST_LIMIT - 1)]
    rows = [{"id": receipt, "sourceInstanceId": OTHER} for receipt in others]
    rows.insert(0, {"id": "rcpt-0000", "sourceInstanceId": NAME})
    runner.answers["trigger list"] = printed(rows)

    search = find_queued(cli, NAME, NAME)

    assert search.complete is False and search.inspected == 1


def test_a_listing_that_is_not_a_list_is_an_error() -> None:
    cli, runner, _queue = queue_cli()
    runner.answers["trigger list"] = printed({"rows": []})

    try:
        find_queued(cli, NAME, NAME)
    except ArchonCliError as error:
        assert error.message == "archon trigger list did not print a list of receipts."
    else:
        raise AssertionError("a listing that is not a list was accepted")


def test_a_full_listing_without_this_binding_says_it_was_not_looked_for(
    tmp_path: Path,
) -> None:
    harness, queue = with_queue(tmp_path)
    for _ in range(3):
        queue.add(OTHER, "admitted")
    rows = [
        {"id": f"rcpt-{n:04d}", "sourceInstanceId": OTHER} for n in range(LIST_LIMIT)
    ]
    harness.runner.answers["trigger list"] = printed(rows)

    stop = harness.client.delete(SCHEDULE, headers=ACTION).json()["stop"]

    assert stop["warnings"] == [
        f"None of Archon's newest {LIST_LIMIT} receipts is from this binding, "
        "so a start it queued before them was not looked for."
    ]


def test_ask_trusts_the_document_over_the_exit_code() -> None:
    runner = FakeRunner()
    cli = ArchonCli(Path("archon"), runner=runner)
    runner.answers["refusal"] = CliResult(0, '{"ok": false, "error": "bad limit"}', "")
    runner.answers["blank"] = CliResult(0, '{"ok": false}', "")
    runner.answers["exit-two"] = CliResult(2, '{"rows": []}', "")
    runner.answers["text"] = CliResult(1, "", "Error: no database")
    runner.answers["fine"] = printed([1, 2])

    def message(command: str) -> str:
        try:
            cli.ask([command])
        except ArchonCliError as error:
            return error.message
        return "no error"

    assert message("refusal") == "bad limit"
    assert message("blank") == "archon blank failed."
    assert message("exit-two") == "archon exit-two exited with code 2."
    assert message("text") == "Error: no database"
    assert cli.ask(["fine"]) == [1, 2]
    assert runner.calls[-1] == ["archon", "fine", "--json"]
