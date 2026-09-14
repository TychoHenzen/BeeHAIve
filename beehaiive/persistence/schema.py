from __future__ import annotations

from typing import Any


class StorageSchemaMixin:
    def _initialize_schema(self: Any) -> None:
        self._connection.executescript(
            """
                PRAGMA foreign_keys = ON;

                CREATE TABLE IF NOT EXISTS projects (
                    project_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS repositories (
                    project_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY (project_id, name),
                    FOREIGN KEY (project_id)
                        REFERENCES projects(project_id)
                        ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS pbi_refinement_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    repository_name TEXT NOT NULL,
                    pbi_number INTEGER NOT NULL CHECK (pbi_number > 0),
                    generation INTEGER NOT NULL CHECK (generation BETWEEN 1 AND 3),
                    reopen_count INTEGER NOT NULL CHECK (reopen_count BETWEEN 0 AND 2),
                    revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
                    status TEXT NOT NULL CHECK (status IN (
                        'awaiting_answers', 'evaluating', 'completed', 'failed'
                    )),
                    questions_json TEXT NOT NULL,
                    decision_json TEXT,
                    failure_reason TEXT,
                    retryable_failure INTEGER NOT NULL DEFAULT 0,
                    history_json TEXT NOT NULL DEFAULT '[]',
                    authorization_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (project_id, repository_name, pbi_number),
                    FOREIGN KEY (project_id, repository_name)
                        REFERENCES repositories(project_id, name)
                        ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS pbis (
                    project_id TEXT NOT NULL,
                    repository_name TEXT NOT NULL,
                    number INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    archived INTEGER NOT NULL DEFAULT 0,
                    run_id TEXT,
                    branch TEXT,
                    pull_request_url TEXT,
                    last_error TEXT,
                    handoff_base_branch TEXT,
                    handoff_body TEXT,
                    handoff_head_sha TEXT,
                    handoff_verification_evidence TEXT,
                    handoff_status TEXT,
                    planning_status TEXT,
                    claimable INTEGER NOT NULL DEFAULT 1,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY (project_id, repository_name, number),
                    FOREIGN KEY (project_id, repository_name)
                        REFERENCES repositories(project_id, name) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    repository_name TEXT NOT NULL,
                    pbi_number INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    owner_id TEXT,
                    lease_token TEXT,
                    lease_expires_at TEXT,
                    execution_token TEXT,
                    last_error TEXT,
                    last_result TEXT,
                    task_contract_json TEXT,
                    task_result_json TEXT,
                    task_answer TEXT,
                    task_answer_resumed INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    UNIQUE (project_id, repository_name, pbi_number),
                    FOREIGN KEY (project_id, repository_name, pbi_number)
                        REFERENCES pbis(project_id, repository_name, number)
                        ON DELETE CASCADE
                );

                CREATE UNIQUE INDEX IF NOT EXISTS active_writer_per_repository
                    ON runs(project_id, repository_name)
                    WHERE status = 'active';

                CREATE TABLE IF NOT EXISTS operator_questions (
                    question_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    repository_name TEXT NOT NULL,
                    pbi_number INTEGER NOT NULL,
                    run_id TEXT NOT NULL,
                    revision INTEGER NOT NULL CHECK (revision > 0),
                    kind TEXT NOT NULL CHECK (
                        kind IN ('question', 'routing_exhausted')
                    ),
                    status TEXT NOT NULL CHECK (
                        status IN ('pending', 'answered', 'closed')
                    ),
                    question TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    answer TEXT,
                    owner_scope TEXT NOT NULL,
                    authorization_method TEXT NOT NULL DEFAULT 'X-API-Key',
                    operator_role TEXT NOT NULL DEFAULT 'operator',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    answered_at TEXT,
                    notification_status TEXT NOT NULL DEFAULT 'pending',
                    notification_attempts INTEGER NOT NULL DEFAULT 0,
                    notification_last_attempt_at TEXT,
                    notification_last_status_code INTEGER,
                    notification_delivered_at TEXT,
                    notification_last_error TEXT,
                    notification_lease_token TEXT,
                    notification_lease_expires_at TEXT,
                    UNIQUE (run_id, revision),
                    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE,
                    FOREIGN KEY (project_id, repository_name, pbi_number)
                        REFERENCES pbis(project_id, repository_name, number)
                        ON DELETE CASCADE
                );

                CREATE UNIQUE INDEX IF NOT EXISTS one_pending_operator_question_per_run
                    ON operator_questions(run_id) WHERE status = 'pending';

                CREATE INDEX IF NOT EXISTS operator_questions_by_project
                    ON operator_questions(project_id, status, created_at DESC);

                CREATE TABLE IF NOT EXISTS events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id TEXT NOT NULL,
                    repository_name TEXT NOT NULL,
                    pbi_number INTEGER NOT NULL,
                    run_id TEXT,
                    event_type TEXT NOT NULL,
                    from_stage TEXT,
                    to_stage TEXT,
                    details_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (project_id, repository_name, pbi_number)
                        REFERENCES pbis(project_id, repository_name, number)
                        ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS agent_sessions (
                    run_id TEXT PRIMARY KEY,
                    worker_id TEXT NOT NULL,
                    task TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS agent_session_events (
                    run_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    role TEXT,
                    text TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    PRIMARY KEY (run_id, sequence),
                    FOREIGN KEY (run_id)
                        REFERENCES agent_sessions(run_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS handoffs (
                    run_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    repository_name TEXT NOT NULL,
                    pbi_number INTEGER NOT NULL,
                    branch TEXT NOT NULL,
                    pull_request_url TEXT NOT NULL,
                    pull_request_number INTEGER,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS actions (
                    action_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    repository_name TEXT,
                    pbi_number INTEGER,
                    run_id TEXT,
                    request_json TEXT NOT NULL,
                    result_json TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS pbi_creations (
                    project_id TEXT NOT NULL,
                    key_hash TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    repository_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    issue_create_started INTEGER NOT NULL DEFAULT 0,
                    issue_id TEXT,
                    issue_number INTEGER,
                    issue_url TEXT,
                    project_item_id TEXT,
                    completed_steps_json TEXT NOT NULL DEFAULT '[]',
                    current_step TEXT,
                    failed_step TEXT,
                    failure_code TEXT,
                    failure_class TEXT,
                    lease_owner TEXT,
                    lease_expires_at TEXT,
                    result_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (project_id, key_hash)
                );

                CREATE TABLE IF NOT EXISTS routing_failure_outbox (
                    transition_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    error TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL,
                    output_tokens INTEGER NOT NULL,
                    recursive_spawn_depth INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    processed_at TEXT,
                    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS routing_failure_outbox_pending
                    ON routing_failure_outbox(run_id, status, created_at);

                CREATE INDEX IF NOT EXISTS actions_by_project
                    ON actions(project_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS meta_review_runs (
                    review_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    record_limit INTEGER NOT NULL,
                    input_token_limit INTEGER NOT NULL,
                    selected_records INTEGER NOT NULL DEFAULT 0,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    missing_evidence_json TEXT NOT NULL DEFAULT '[]',
                    error TEXT,
                    created_at TEXT NOT NULL,
                    completed_at TEXT,
                    FOREIGN KEY (project_id) REFERENCES projects(project_id)
                        ON DELETE CASCADE
                );

                CREATE UNIQUE INDEX IF NOT EXISTS active_meta_review_project
                    ON meta_review_runs(project_id)
                    WHERE status = 'running';

                CREATE UNIQUE INDEX IF NOT EXISTS active_meta_review_global
                    ON meta_review_runs(status)
                    WHERE status = 'running';

                CREATE TABLE IF NOT EXISTS meta_review_suggestions (
                    suggestion_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    suggestion_key TEXT NOT NULL,
                    proposed_outcome TEXT NOT NULL,
                    rationale TEXT NOT NULL,
                    evidence_refs_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_review_id TEXT NOT NULL,
                    UNIQUE (project_id, suggestion_key),
                    FOREIGN KEY (project_id) REFERENCES projects(project_id)
                        ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS meta_review_suggestions_by_project
                    ON meta_review_suggestions(project_id, status, created_at);
                """
        )


__all__ = ["StorageSchemaMixin"]
