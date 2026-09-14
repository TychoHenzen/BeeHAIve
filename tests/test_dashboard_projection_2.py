from types import SimpleNamespace

import main as main_module
from beehaiive.workflow import (
    CheckResult,
    GateResult,
    LeaseStatus,
)


def test_dashboard_delivery_projection_handles_missing_lease_and_gate() -> None:
    workflow_service = SimpleNamespace(workspace_for_run=lambda _run_id: None)
    assert main_module._dashboard_delivery(workflow_service, "missing") is None

    lease = SimpleNamespace(
        lease_id="lease-40",
        branch="codex/40",
        status=LeaseStatus.RETAINED,
    )
    workflow_service.workspace_for_run = lambda _run_id: lease
    workflow_service.store = SimpleNamespace(latest_gate=lambda _lease_id, _gate: None)
    assert main_module._dashboard_delivery(workflow_service, "run-40") is None

    workflow_service.store.latest_gate = lambda _lease_id, _gate: SimpleNamespace(
        checks=(SimpleNamespace(name="other", evidence="value"),)
    )
    assert main_module._dashboard_delivery(workflow_service, "run-40") is None

    workflow_service.store.latest_gate = lambda _lease_id, _gate: SimpleNamespace(
        checks=(
            SimpleNamespace(name="delivery_status", evidence="push_failed"),
            SimpleNamespace(name="commit_sha", evidence="abc123"),
            SimpleNamespace(name="evidence", evidence="local commit preserved"),
        )
    )
    delivery = main_module._dashboard_delivery(workflow_service, "run-40")
    assert delivery == {
        "status": "push_failed",
        "commit_sha": "abc123",
        "branch": "codex/40",
        "evidence": "local commit preserved",
        "retry_available": True,
    }


def test_dashboard_quality_gate_projection_exposes_full_check_evidence() -> None:
    lease = SimpleNamespace(lease_id="lease-42")
    check = CheckResult(
        "unit-tests",
        False,
        "Command exited with status 1",
        status="failed",
        category="tests",
        required=True,
        argv=("uv", "run", "pytest"),
        exit_code=1,
        stdout="1 failed",
        stderr="",
        error="Command exited with status 1",
    )
    gate = GateResult("model_call", False, (check,), "Repair the failed checks")
    workflow_service = SimpleNamespace(
        workspace_for_run=lambda _run_id: lease,
        store=SimpleNamespace(
            latest_gate=lambda _lease_id, name: gate if name == "model_call" else None
        ),
    )

    assert main_module._dashboard_quality_gates(workflow_service, "run-42") == {
        "model_call": gate.as_dict()
    }
    assert main_module._dashboard_quality_gate_summary(workflow_service, "run-42") == {
        "model_call": {
            "gate": "model_call",
            "allowed": False,
            "checks": [
                {
                    "name": "unit-tests",
                    "passed": False,
                    "status": "failed",
                    "category": "tests",
                    "required": True,
                    "exit_code": 1,
                }
            ],
        }
    }
