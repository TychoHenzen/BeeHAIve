from __future__ import annotations

from typing import Any


class StorageMigrationMixin:
    def _migrate_schema(self: Any) -> None:
        repository_columns = {
            str(row["name"])
            for row in self._connection.execute(
                "PRAGMA table_info(repositories)"
            ).fetchall()
        }
        if "active" not in repository_columns:
            self._connection.execute(
                """
                    ALTER TABLE repositories
                    ADD COLUMN active INTEGER NOT NULL DEFAULT 1
                    """
            )
        pbi_columns = {
            str(row["name"])
            for row in self._connection.execute("PRAGMA table_info(pbis)").fetchall()
        }
        if "active" not in pbi_columns:
            self._connection.execute(
                "ALTER TABLE pbis ADD COLUMN active INTEGER NOT NULL DEFAULT 1"
            )
        run_columns = {
            str(row["name"])
            for row in self._connection.execute("PRAGMA table_info(runs)").fetchall()
        }
        for column in (
            "owner_id",
            "lease_token",
            "lease_expires_at",
            "execution_token",
            "last_result",
            "task_contract_json",
            "task_result_json",
            "task_answer",
        ):
            if column not in run_columns:
                self._connection.execute(f"ALTER TABLE runs ADD COLUMN {column} TEXT")
        if "task_answer_resumed" not in run_columns:
            self._connection.execute(
                "ALTER TABLE runs ADD COLUMN task_answer_resumed "
                "INTEGER NOT NULL DEFAULT 0"
            )
        for column in (
            "handoff_base_branch",
            "handoff_body",
            "handoff_head_sha",
            "handoff_verification_evidence",
            "handoff_status",
            "planning_status",
            "claimable",
            "metadata_json",
            "archived",
        ):
            if column not in pbi_columns:
                definition = (
                    "INTEGER NOT NULL DEFAULT 1"
                    if column == "claimable"
                    else "INTEGER NOT NULL DEFAULT 0"
                    if column == "archived"
                    else "TEXT NOT NULL DEFAULT '{}'"
                    if column == "metadata_json"
                    else "TEXT"
                )
                self._connection.execute(
                    f"ALTER TABLE pbis ADD COLUMN {column} {definition}"
                )


__all__ = ["StorageMigrationMixin"]
