from __future__ import annotations

from pathlib import Path

import pytest

from beehaiive.workflow import (
    RepairStatus,
    WorkflowError,
    WorkflowStore,
)

CONSTITUTION_PATH = Path(__file__).parents[1] / "constitution.json"


def test_workflow_store_rejects_invalid_repair_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = WorkflowStore(tmp_path / "workflow.sqlite3")
    evidence = {
        "before": {"source_head": "head-1"},
    }
    try:
        with pytest.raises(WorkflowError, match="positive"):
            store.begin_repair(
                "invalid-number",
                "PR_invalid",
                "owner/repo",
                0,
                "feature",
                "target",
                "head-1",
                "base-1",
                "codex/repair-invalid",
                str(tmp_path / "invalid"),
                evidence,
            )
        record = store.begin_repair(
            "repair-state",
            "PR_state",
            "owner/repo",
            7,
            "feature",
            "target",
            "head-state",
            "base-state",
            "codex/repair-state",
            str(tmp_path / "state"),
            evidence,
        )
        with store._transaction() as connection:
            connection.execute(
                "UPDATE workflow_repairs SET evidence_json = ? WHERE repair_id = ?",
                ("not-json", record.repair_id),
            )
        with pytest.raises(WorkflowError, match="evidence"):
            store.get_repair(record.repair_id)

        with store._transaction() as connection:
            connection.execute(
                "UPDATE workflow_repairs SET evidence_json = ? WHERE repair_id = ?",
                ("[]", record.repair_id),
            )
        with pytest.raises(WorkflowError, match="evidence"):
            store.get_repair(record.repair_id)

        with store._transaction() as connection:
            connection.execute(
                "UPDATE workflow_repairs SET evidence_json = ?, status = ? "
                "WHERE repair_id = ?",
                ("{}", "invalid", record.repair_id),
            )
        with pytest.raises(WorkflowError, match="status"):
            store.get_repair(record.repair_id)

        with store._transaction() as connection:
            connection.execute(
                "UPDATE workflow_repairs SET status = ? WHERE repair_id = ?",
                (RepairStatus.RUNNING.value, record.repair_id),
            )
        with pytest.raises(WorkflowError, match="Unknown repair"):
            store.attach_repair_workspace("missing", "lease", str(tmp_path / "missing"))
        attached = store.attach_repair_workspace(
            record.repair_id, "lease-1", str(tmp_path / "state-worktree")
        )
        assert attached.lease_id == "lease-1"
        with pytest.raises(WorkflowError, match="different workspace"):
            store.attach_repair_workspace(
                record.repair_id, "lease-2", str(tmp_path / "other-worktree")
            )

        disappearing = store.begin_repair(
            "repair-disappears",
            "PR_disappears",
            "owner/repo",
            8,
            "feature",
            "target",
            "head-disappears",
            "base-disappears",
            "codex/repair-disappears",
            str(tmp_path / "disappears"),
            {},
        )
        monkeypatch.setattr(store, "get_repair", lambda repair_id: None)
        with pytest.raises(WorkflowError, match="disappeared"):
            store.attach_repair_workspace(
                disappearing.repair_id, "lease-disappears", str(tmp_path / "gone")
            )
        monkeypatch.undo()

        finished = store.finish_repair(
            record.repair_id,
            RepairStatus.SUCCEEDED,
            None,
            {"finished": True},
            repaired_head="head-fixed",
        )
        assert finished.status is RepairStatus.SUCCEEDED
        assert (
            store.finish_repair(
                record.repair_id,
                RepairStatus.BLOCKED,
                "ignored",
                {"ignored": True},
            )
            == finished
        )
        with pytest.raises(WorkflowError, match="Unknown repair"):
            store.finish_repair("missing", RepairStatus.BLOCKED, "missing", {})

        finishing_disappears = store.begin_repair(
            "repair-finishing-disappears",
            "PR_finishing-disappears",
            "owner/repo",
            9,
            "feature",
            "target",
            "head-finishing-disappears",
            "base-finishing-disappears",
            "codex/repair-finishing-disappears",
            str(tmp_path / "finishing-disappears"),
            {},
        )
        monkeypatch.setattr(store, "get_repair", lambda repair_id: None)
        with pytest.raises(WorkflowError, match="disappeared"):
            store.finish_repair(
                finishing_disappears.repair_id,
                RepairStatus.BLOCKED,
                "gone",
                {},
            )
        monkeypatch.undo()

        with store._transaction() as connection:
            connection.execute(
                """
                CREATE TRIGGER delete_repair_after_insert
                AFTER INSERT ON workflow_repairs
                BEGIN
                    DELETE FROM workflow_repairs WHERE repair_id = NEW.repair_id;
                END
                """
            )
        with pytest.raises(WorkflowError, match="persisted"):
            store.begin_repair(
                "repair-not-persisted",
                "PR_not-persisted",
                "owner/repo",
                10,
                "feature",
                "target",
                "head-not-persisted",
                "base-not-persisted",
                "codex/repair-not-persisted",
                str(tmp_path / "not-persisted"),
                {},
            )
    finally:
        store.close()
