import subprocess

import pytest

import beehaiive.routing as routing_module
import beehaiive.storage as storage_module
from scripts.dashboard_smoke import (
    SmokeFailure,
    build_app,
    terminate_process,
)
from scripts.dashboard_smoke_browser import set_input
from scripts.dashboard_smoke_proof import (
    action_run_target,
    configured_live_timeout,
)


def test_build_app_closes_stores_when_construction_fails(monkeypatch, tmp_path) -> None:
    class TrackedStore:
        instances = []

        def __init__(self, database) -> None:
            self.database = database
            self.closed = False
            self.__class__.instances.append(self)

        def close(self) -> None:
            self.closed = True

    def fail_routing_store(*args, **kwargs):
        raise RuntimeError("routing construction failed")

    monkeypatch.setattr(storage_module, "OrchestratorStore", TrackedStore)
    monkeypatch.setattr(routing_module, "RoutingStore", fail_routing_store)

    with pytest.raises(RuntimeError, match="routing construction failed"):
        build_app("fixture", "fixture:1", tmp_path)

    assert len(TrackedStore.instances) == 1
    assert TrackedStore.instances[0].closed is True


def test_terminate_process_reports_a_stuck_browser() -> None:
    class StuckProcess:
        def poll(self):
            return None

        def terminate(self) -> None:
            return None

        def kill(self) -> None:
            return None

        def wait(self, timeout: float) -> None:
            raise subprocess.TimeoutExpired("browser", timeout)

    with pytest.raises(SmokeFailure, match="did not terminate"):
        terminate_process(StuckProcess())


def test_action_target_uses_the_run_returned_by_the_action() -> None:
    response = {
        "payload": {
            "result": {
                "run": {
                    "repository": "owner/api",
                    "pbi_number": 7,
                    "run_id": "run-7",
                    "attempt": 2,
                }
            }
        }
    }

    assert action_run_target(response, "start_writer") == (
        "owner/api",
        7,
        "run-7",
        2,
    )


def test_set_input_fails_at_a_missing_control() -> None:
    class MissingInputDevTools:
        def evaluate(self, expression: str):
            return False

    with pytest.raises(SmokeFailure, match="was not rendered"):
        set_input(MissingInputDevTools(), "#settings-project-id", "owner:7")


def test_live_timeout_uses_provider_deadline_or_explicit_value() -> None:
    assert configured_live_timeout(None) == 65.0
    assert configured_live_timeout(90.0) == 90.0
