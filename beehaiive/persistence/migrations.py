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
        question_columns = {
            str(row["name"])
            for row in self._connection.execute(
                "PRAGMA table_info(operator_questions)"
            ).fetchall()
        }
        for column, definition in (
            ("authorization_method", "TEXT NOT NULL DEFAULT 'X-API-Key'"),
            ("operator_role", "TEXT NOT NULL DEFAULT 'operator'"),
            ("notification_last_status_code", "INTEGER"),
        ):
            if column not in question_columns:
                self._connection.execute(
                    f"ALTER TABLE operator_questions ADD COLUMN {column} {definition}"
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
        if "canonical_state" not in pbi_columns:
            self._connection.execute(
                "ALTER TABLE pbis ADD COLUMN canonical_state TEXT NOT NULL "
                "DEFAULT 'unknown'"
            )
        if "canonical_facts_json" not in pbi_columns:
            self._connection.execute(
                "ALTER TABLE pbis ADD COLUMN canonical_facts_json TEXT NOT NULL "
                "DEFAULT '{}'"
            )
        if "canonical_source_version" not in pbi_columns:
            self._connection.execute(
                "ALTER TABLE pbis ADD COLUMN canonical_source_version TEXT NOT NULL "
                "DEFAULT ''"
            )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS lifecycle_transition_evidence (
                evidence_id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL,
                repository_name TEXT NOT NULL,
                pbi_number INTEGER NOT NULL,
                replay_id TEXT NOT NULL UNIQUE,
                event_type TEXT NOT NULL,
                source_owner TEXT NOT NULL,
                source_id TEXT NOT NULL,
                source_version TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                state_before TEXT NOT NULL,
                state_after TEXT NOT NULL,
                reason_code TEXT NOT NULL,
                schema_version INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (project_id, repository_name, pbi_number)
                    REFERENCES pbis(project_id, repository_name, number)
                    ON DELETE CASCADE
            )
            """
        )
        self._connection.execute(
            """
            CREATE INDEX IF NOT EXISTS lifecycle_evidence_by_pbi
                ON lifecycle_transition_evidence(
                    project_id, repository_name, pbi_number, evidence_id
                )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS graph_definitions (
                definition_id INTEGER PRIMARY KEY AUTOINCREMENT,
                workflow_id TEXT NOT NULL,
                revision INTEGER NOT NULL CHECK (revision > 0),
                schema_version INTEGER NOT NULL CHECK (schema_version > 0),
                definition_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (workflow_id, revision)
            )
            """
        )
        self._connection.execute(
            """
            CREATE INDEX IF NOT EXISTS graph_definitions_by_workflow
                ON graph_definitions(workflow_id, revision)
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS graph_transitions (
                transition_id INTEGER PRIMARY KEY AUTOINCREMENT,
                replay_id TEXT NOT NULL UNIQUE,
                execution_id TEXT NOT NULL,
                workflow_id TEXT NOT NULL,
                revision INTEGER NOT NULL,
                node_id TEXT NOT NULL,
                step INTEGER NOT NULL,
                attempt INTEGER NOT NULL,
                transition_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        self._connection.execute(
            """
            CREATE INDEX IF NOT EXISTS graph_transitions_by_execution
                ON graph_transitions(execution_id, transition_id)
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS graph_transition_claims (
                replay_id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                claimed_at TEXT NOT NULL
            )
            """
        )
        claim_columns = {
            str(row["name"])
            for row in self._connection.execute(
                "PRAGMA table_info(graph_transition_claims)"
            ).fetchall()
        }
        if "owner_id" not in claim_columns:
            self._connection.execute(
                "ALTER TABLE graph_transition_claims ADD COLUMN owner_id TEXT "
                "NOT NULL DEFAULT ''"
            )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS budget_decision_evidence (
                evidence_id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL,
                replay_id TEXT NOT NULL UNIQUE,
                source_id TEXT NOT NULL,
                source_version TEXT NOT NULL,
                evidence_status TEXT NOT NULL,
                action TEXT NOT NULL,
                reason_code TEXT NOT NULL,
                fallback_model TEXT,
                observed_at TEXT NOT NULL,
                snapshot_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (project_id)
                    REFERENCES projects(project_id) ON DELETE CASCADE
            )
            """
        )
        budget_columns = {
            str(row["name"])
            for row in self._connection.execute(
                "PRAGMA table_info(budget_decision_evidence)"
            ).fetchall()
        }
        if "fallback_model" not in budget_columns:
            self._connection.execute(
                "ALTER TABLE budget_decision_evidence ADD COLUMN fallback_model TEXT"
            )
        self._connection.execute(
            """
            CREATE INDEX IF NOT EXISTS budget_evidence_by_project
                ON budget_decision_evidence(project_id, evidence_id)
            """
        )


__all__ = ["StorageMigrationMixin"]
