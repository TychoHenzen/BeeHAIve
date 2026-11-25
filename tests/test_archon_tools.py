from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from beehaiive.archon.cli import ArchonCliError, CliResult
from tests.archon_fakes import ACTION, ArchonHarness, archon_harness, printed


def form(cwd: Path, **changes: object) -> dict[str, object]:
    base: dict[str, object] = {
        "name": "repo-build-queue",
        "workflow": "archon-build-queue",
        "cwd": str(cwd),
        "resource": "forge:github.com/o/repo:build",
        "hostId": "local",
        "runAsUserId": "u1",
        "intervalSeconds": 1800,
        "inputs": {"repo_host": "github.com"},
        "overlap": "skip",
        "capacity": 2,
        "isolation": "worktree",
    }
    return base | changes


def with_binding(tmp_path: Path) -> ArchonHarness:
    harness = archon_harness(tmp_path)
    made = harness.client.post(
        "/api/archon/triggers/bindings", json=form(tmp_path), headers=ACTION
    )
    assert made.status_code == 200
    return harness


def test_settings_say_where_archon_is(tmp_path: Path) -> None:
    harness = archon_harness(tmp_path)

    settings = harness.client.get("/api/archon/settings").json()

    assert settings["url"] == "http://archon.test"
    assert settings["home"] == str(harness.home)
    assert settings["defaultHome"] is False


def test_identity_comes_from_trigger_whoami(tmp_path: Path) -> None:
    harness = archon_harness(tmp_path)
    harness.runner.answers["trigger whoami"] = printed({"runAsUserId": "u1"})

    assert harness.client.get("/api/archon/identity").json() == {
        "identity": {"runAsUserId": "u1"}
    }


def test_a_cli_failure_is_a_502_with_its_output(tmp_path: Path) -> None:
    harness = archon_harness(tmp_path)
    harness.runner.answers["whoami"] = CliResult(1, "", "no database")

    answer = harness.client.get("/api/archon/identity")

    assert answer.status_code == 502
    assert answer.json() == {"error": "no database", "detail": "no database"}


def test_a_binding_is_created_and_listed(tmp_path: Path) -> None:
    harness = with_binding(tmp_path)

    listing = harness.client.get("/api/archon/triggers/bindings").json()

    assert listing["directory"] == str(harness.home / "triggers")
    assert [b["name"] for b in listing["bindings"]] == ["repo-build-queue"]
    written = json.loads(
        (harness.home / "triggers" / "repo-build-queue.json").read_text("utf-8")
    )
    assert written["binding"]["runAsUserId"] == "u1"


def test_a_bad_binding_is_a_400(tmp_path: Path) -> None:
    harness = archon_harness(tmp_path)

    answer = harness.client.post(
        "/api/archon/triggers/bindings",
        json=form(tmp_path, intervalSeconds=5),
        headers=ACTION,
    )

    assert answer.status_code == 400
    assert "once a minute" in answer.json()["error"]


def test_fire_runs_the_binding_file(tmp_path: Path) -> None:
    harness = with_binding(tmp_path)
    harness.runner.answers["trigger fire"] = CliResult(0, "admitted r1", "")

    fired = harness.client.post(
        "/api/archon/triggers/bindings/repo-build-queue/fire", headers=ACTION
    ).json()

    assert fired == {"ok": True, "code": 0, "output": "admitted r1"}
    config = str(harness.home / "triggers" / "repo-build-queue.json")
    assert harness.runner.calls[-1][1:] == ["trigger", "fire", "--config", config]
    # The same folder the scheduled task starts in.
    assert harness.runner.cwds[-1] == tmp_path


def test_fire_runs_without_a_folder_when_the_binding_has_none(tmp_path: Path) -> None:
    harness = archon_harness(tmp_path)
    triggers = harness.home / "triggers"
    triggers.mkdir()
    (triggers / "loose.json").write_text(json.dumps({"binding": {}}), "utf-8")
    harness.runner.answers["trigger fire"] = CliResult(1, "", "Invalid timer")

    fired = harness.client.post(
        "/api/archon/triggers/bindings/loose/fire", headers=ACTION
    ).json()

    assert fired == {"ok": False, "code": 1, "output": "Invalid timer"}
    assert harness.runner.cwds == [None]


def test_firing_a_missing_binding_is_a_404(tmp_path: Path) -> None:
    harness = archon_harness(tmp_path)

    answer = harness.client.post(
        "/api/archon/triggers/bindings/nope/fire", headers=ACTION
    )

    assert answer.status_code == 404
    assert answer.json()["error"] == "No such binding."
    assert harness.runner.calls == []


def test_a_fire_that_cannot_start_is_a_502(tmp_path: Path) -> None:
    harness = with_binding(tmp_path)
    harness.runner.error = ArchonCliError("archon was not found.")

    answer = harness.client.post(
        "/api/archon/triggers/bindings/repo-build-queue/fire", headers=ACTION
    )

    assert answer.status_code == 502


def test_receipts_are_listed_and_inspected(tmp_path: Path) -> None:
    harness = archon_harness(tmp_path)
    harness.runner.answers["trigger list"] = printed([{"receiptId": "a1"}])
    harness.runner.answers["trigger inspect a1"] = printed({"status": "admitted"})
    harness.runner.answers["trigger inspect b2"] = CliResult(1, "", "No receipt b2")

    listed = harness.client.get("/api/archon/triggers/receipts?limit=5000").json()
    found = harness.client.get("/api/archon/triggers/receipts/a1").json()
    missing = harness.client.get("/api/archon/triggers/receipts/b2").json()

    assert listed == {"receipts": [{"receiptId": "a1"}]}
    assert harness.runner.calls[0][-2:] == ["--limit", "1000"]
    assert found == {"ok": True, "receipt": {"status": "admitted"}}
    assert missing == {"ok": False, "text": "No receipt b2"}


def test_a_receipt_id_cannot_look_like_an_option(tmp_path: Path) -> None:
    harness = archon_harness(tmp_path)

    answer = harness.client.get("/api/archon/triggers/receipts/--help")

    assert answer.status_code == 400
    assert harness.runner.calls == []


def test_a_receipt_failure_is_a_502(tmp_path: Path) -> None:
    harness = archon_harness(tmp_path)

    assert harness.client.get("/api/archon/triggers/receipts").status_code == 502
    harness.runner.error = ArchonCliError("archon was not found.")
    assert harness.client.get("/api/archon/triggers/receipts/a1").status_code == 502


def test_schedules_list_install_and_remove(tmp_path: Path) -> None:
    harness = with_binding(tmp_path)
    scheduler = harness.scheduler_runner
    scheduler.answers["-Action List"] = printed([{"name": "Archon-repo-build-queue"}])
    scheduler.answers["-Action Install"] = CliResult(0, "installed", "")
    scheduler.answers["-Action Remove"] = CliResult(0, "removed", "")
    path = "/api/archon/triggers/bindings/repo-build-queue/schedule"

    listed = harness.client.get("/api/archon/triggers/schedules").json()
    installed = harness.client.put(path, headers=ACTION).json()
    removed = harness.client.delete(path, headers=ACTION).json()

    assert listed == {"supported": True, "tasks": [{"name": "Archon-repo-build-queue"}]}
    assert installed == {"installed": True} and removed == {"removed": True}
    install = scheduler.calls[1]
    assert install[install.index("-IntervalSeconds") + 1] == "1800"
    assert install[install.index("-WorkingDirectory") + 1] == str(tmp_path)


BINDING = "/api/archon/triggers/bindings/repo-build-queue"
BINDING_FILE = Path("triggers") / "repo-build-queue.json"


def test_deleting_a_binding_removes_its_task_then_its_file(tmp_path: Path) -> None:
    harness = with_binding(tmp_path)
    scheduler = harness.scheduler_runner
    scheduler.answers["-Action List"] = printed([{"name": "Archon-repo-build-queue"}])
    scheduler.answers["-Action Remove"] = CliResult(0, "removed", "")

    deleted = harness.client.delete(BINDING, headers=ACTION).json()

    assert deleted == {"deleted": True, "unscheduled": True}
    assert not (harness.home / BINDING_FILE).exists()
    remove = scheduler.calls[-1]
    assert remove[remove.index("-Name") + 1] == "Archon-repo-build-queue"
    listing = harness.client.get("/api/archon/triggers/bindings").json()
    assert listing["bindings"] == []


def test_deleting_an_unscheduled_binding_touches_no_task(tmp_path: Path) -> None:
    harness = with_binding(tmp_path)
    scheduler = harness.scheduler_runner
    scheduler.answers["-Action List"] = printed([{"name": "Archon-other"}])

    deleted = harness.client.delete(BINDING, headers=ACTION).json()

    assert deleted == {"deleted": True, "unscheduled": False}
    assert not (harness.home / BINDING_FILE).exists()
    assert not any("Remove" in call for call in scheduler.calls)


def test_a_binding_whose_task_will_not_go_stays(tmp_path: Path) -> None:
    harness = with_binding(tmp_path)
    scheduler = harness.scheduler_runner
    scheduler.answers["-Action List"] = printed([{"name": "Archon-repo-build-queue"}])
    scheduler.answers["-Action Remove"] = CliResult(1, "", "Access denied")

    answer = harness.client.delete(BINDING, headers=ACTION)

    assert answer.status_code == 502
    assert answer.json()["detail"] == "Access denied"
    assert (harness.home / BINDING_FILE).is_file()


def test_a_file_that_will_not_delete_is_logged_not_echoed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    harness = archon_harness(tmp_path, platform="linux")
    harness.client.post(
        "/api/archon/triggers/bindings", json=form(tmp_path), headers=ACTION
    )

    def refuse(_path: Path, missing_ok: bool = False) -> None:
        raise PermissionError(13, "Access is denied", "C:/hidden/binding.json")

    monkeypatch.setattr(Path, "unlink", refuse)
    answer = harness.client.delete(BINDING, headers=ACTION)

    assert answer.status_code == 500
    assert answer.json() == {
        "error": "The binding file could not be deleted. BeeHAIve's log has why."
    }
    assert "hidden" in caplog.text


def test_deleting_a_missing_or_misnamed_binding_is_a_404(tmp_path: Path) -> None:
    harness = archon_harness(tmp_path)
    base = "/api/archon/triggers/bindings"

    missing = harness.client.delete(f"{base}/nope", headers=ACTION)
    bad_name = harness.client.delete(f"{base}/..bad", headers=ACTION)

    assert missing.status_code == 404 and bad_name.status_code == 404
    assert harness.scheduler_runner.calls == []


def test_deleting_a_binding_off_windows_asks_no_scheduler(tmp_path: Path) -> None:
    harness = archon_harness(tmp_path, platform="linux")
    harness.client.post(
        "/api/archon/triggers/bindings", json=form(tmp_path), headers=ACTION
    )

    deleted = harness.client.delete(BINDING, headers=ACTION).json()

    assert deleted == {"deleted": True, "unscheduled": False}
    assert harness.scheduler_runner.calls == []


def test_scheduling_off_windows_says_so(tmp_path: Path) -> None:
    harness = archon_harness(tmp_path, platform="linux")

    listed = harness.client.get("/api/archon/triggers/schedules").json()

    assert listed == {"supported": False, "tasks": []}


def test_schedule_errors_map_to_status_codes(tmp_path: Path) -> None:
    harness = with_binding(tmp_path)
    triggers = harness.home / "triggers"
    (triggers / "loose.json").write_text(json.dumps({"binding": {}}), "utf-8")
    harness.scheduler_runner.answers["-Action"] = CliResult(1, "", "Access denied")
    base = "/api/archon/triggers/bindings"

    missing = harness.client.put(f"{base}/nope/schedule", headers=ACTION)
    loose = harness.client.put(f"{base}/loose/schedule", headers=ACTION)
    denied = harness.client.put(f"{base}/repo-build-queue/schedule", headers=ACTION)
    bad_name = harness.client.delete(f"{base}/..bad/schedule", headers=ACTION)
    remove_denied = harness.client.delete(
        f"{base}/repo-build-queue/schedule", headers=ACTION
    )
    list_denied = harness.client.get("/api/archon/triggers/schedules")

    assert missing.status_code == 404
    assert loose.status_code == 400
    assert denied.status_code == 502 and denied.json()["detail"] == "Access denied"
    assert bad_name.status_code == 404
    assert remove_denied.status_code == 502
    assert list_denied.status_code == 502


def edits(cwd: Path, **changes: object) -> dict[str, object]:
    base = form(cwd)
    for fixed in ("name", "runAsUserId"):
        base.pop(fixed)
    return base | changes


def test_an_edit_changes_capacity_and_keeps_what_the_form_does_not_know(
    tmp_path: Path,
) -> None:
    harness = with_binding(tmp_path)
    path = harness.home / BINDING_FILE
    document = json.loads(path.read_text("utf-8"))
    document["binding"]["launch"]["discoveryCwd"] = str(tmp_path)
    document["schedule"]["runAtLoad"] = True
    path.write_text(json.dumps(document), "utf-8")

    edited = harness.client.put(
        BINDING, json=edits(tmp_path, capacity=4, overlap="queue"), headers=ACTION
    ).json()

    assert edited["rescheduled"] is False
    assert edited["binding"]["capacity"] == 4
    assert edited["binding"]["overlap"] == "queue"
    written = json.loads(path.read_text("utf-8"))
    assert written["binding"]["runAsUserId"] == "u1"
    assert written["binding"]["bindingId"] == "repo-build-queue"
    assert written["binding"]["launch"]["discoveryCwd"] == str(tmp_path)
    assert written["schedule"] == {"intervalSeconds": 1800, "runAtLoad": True}
    # Only the interval and folder live in the Windows task.
    assert harness.scheduler_runner.calls == []
    assert [p.name for p in path.parent.iterdir()] == ["repo-build-queue.json"]


def test_a_new_interval_reinstalls_the_bindings_windows_task(tmp_path: Path) -> None:
    harness = with_binding(tmp_path)
    scheduler = harness.scheduler_runner
    scheduler.answers["-Action List"] = printed([{"name": "Archon-repo-build-queue"}])
    scheduler.answers["-Action Install"] = CliResult(0, "installed", "")

    edited = harness.client.put(
        BINDING, json=edits(tmp_path, intervalSeconds=3600), headers=ACTION
    ).json()

    assert edited["rescheduled"] is True
    install = scheduler.calls[-1]
    assert install[install.index("-IntervalSeconds") + 1] == "3600"
    assert install[install.index("-Name") + 1] == "Archon-repo-build-queue"


def test_a_new_interval_without_a_task_schedules_nothing(tmp_path: Path) -> None:
    harness = with_binding(tmp_path)
    harness.scheduler_runner.answers["-Action List"] = printed([])

    edited = harness.client.put(
        BINDING, json=edits(tmp_path, intervalSeconds=3600), headers=ACTION
    ).json()

    assert edited["rescheduled"] is False
    assert edited["binding"]["intervalSeconds"] == 3600
    assert not any("Install" in call for call in harness.scheduler_runner.calls)


def test_a_task_that_will_not_reinstall_says_the_file_is_saved(tmp_path: Path) -> None:
    harness = with_binding(tmp_path)
    scheduler = harness.scheduler_runner
    scheduler.answers["-Action List"] = printed([{"name": "Archon-repo-build-queue"}])
    scheduler.answers["-Action Install"] = CliResult(1, "", "Access denied")

    answer = harness.client.put(
        BINDING, json=edits(tmp_path, intervalSeconds=3600), headers=ACTION
    )

    assert answer.status_code == 502
    assert "saved" in answer.json()["error"]
    assert answer.json()["detail"] == "Access denied"
    written = json.loads((harness.home / BINDING_FILE).read_text("utf-8"))
    assert written["schedule"]["intervalSeconds"] == 3600


def test_a_bad_edit_is_a_400_and_leaves_the_file(tmp_path: Path) -> None:
    harness = with_binding(tmp_path)
    before = (harness.home / BINDING_FILE).read_text("utf-8")

    answer = harness.client.put(
        BINDING, json=edits(tmp_path, capacity=0), headers=ACTION
    )

    assert answer.status_code == 400
    assert "Capacity" in answer.json()["error"]
    assert (harness.home / BINDING_FILE).read_text("utf-8") == before


def test_editing_a_missing_misnamed_or_broken_binding_is_refused(
    tmp_path: Path,
) -> None:
    harness = archon_harness(tmp_path)
    triggers = harness.home / "triggers"
    triggers.mkdir()
    (triggers / "loose.json").write_text("[]", "utf-8")
    base = "/api/archon/triggers/bindings"

    missing = harness.client.put(f"{base}/nope", json=edits(tmp_path), headers=ACTION)
    bad_name = harness.client.put(f"{base}/..bad", json=edits(tmp_path), headers=ACTION)
    loose = harness.client.put(f"{base}/loose", json=edits(tmp_path), headers=ACTION)

    assert missing.status_code == 404 and bad_name.status_code == 404
    assert loose.status_code == 400
    assert (triggers / "loose.json").read_text("utf-8") == "[]"


def test_an_edit_without_the_action_header_is_refused(tmp_path: Path) -> None:
    harness = with_binding(tmp_path)

    answer = harness.client.put(BINDING, json=edits(tmp_path, capacity=4))

    assert answer.status_code == 403
    written = json.loads((harness.home / BINDING_FILE).read_text("utf-8"))
    assert written["binding"]["capacity"] == 2


def test_pause_and_resume_change_every_task(tmp_path: Path) -> None:
    harness = archon_harness(tmp_path)
    scheduler = harness.scheduler_runner
    scheduler.answers["-Action Pause"] = CliResult(0, "7", "")
    scheduler.answers["-Action Resume"] = CliResult(0, "7", "")

    paused = harness.client.post("/api/archon/triggers/pause", headers=ACTION)
    resumed = harness.client.post("/api/archon/triggers/resume", headers=ACTION)

    assert paused.json() == {"paused": 7} and resumed.json() == {"resumed": 7}


def test_pause_without_the_action_header_is_refused(tmp_path: Path) -> None:
    harness = archon_harness(tmp_path)

    answer = harness.client.post("/api/archon/triggers/pause")

    assert answer.status_code == 403
    assert harness.scheduler_runner.calls == []


def test_a_refused_pause_is_a_502_with_its_output(tmp_path: Path) -> None:
    harness = archon_harness(tmp_path)
    harness.scheduler_runner.answers["-Action Pause"] = CliResult(1, "", "denied")

    answer = harness.client.post("/api/archon/triggers/pause", headers=ACTION)

    assert answer.status_code == 502
    assert answer.json() == {
        "error": "Task Scheduler refused the change.",
        "detail": "denied",
    }


def test_processes_name_this_host_and_its_archon_pids(tmp_path: Path) -> None:
    harness = archon_harness(tmp_path)
    listing = '"archon.exe","47492","Console","1","48.640 K"'
    harness.scheduler_runner.answers["tasklist"] = CliResult(0, listing, "")

    answer = harness.client.get("/api/archon/triggers/processes").json()

    assert answer == {"supported": True, "host": socket.gethostname(), "pids": [47492]}


def test_processes_off_windows_say_so_and_run_nothing(tmp_path: Path) -> None:
    harness = archon_harness(tmp_path, platform="linux")

    answer = harness.client.get("/api/archon/triggers/processes").json()

    assert answer["supported"] is False and answer["pids"] == []
    assert harness.scheduler_runner.calls == []


def test_a_failed_process_listing_is_a_502(tmp_path: Path) -> None:
    harness = archon_harness(tmp_path)
    harness.scheduler_runner.answers["tasklist"] = CliResult(1, "", "denied")

    assert harness.client.get("/api/archon/triggers/processes").status_code == 502
