"""Durable isolation, handoff, and deterministic quality gates."""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
from collections.abc import Callable, Generator, Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from threading import RLock
from types import MappingProxyType
from typing import Protocol, cast
from uuid import uuid4


class WorkflowError(RuntimeError):
    """Raised when a workflow cannot safely advance."""


class WorkflowRole(StrEnum):
    """Roles that can participate in a committed workflow handoff."""

    PLANNER = "planner"
    WRITER = "writer"
    REVIEWER = "reviewer"
    OPERATOR = "operator"


class LeaseStatus(StrEnum):
    """Lifecycle state of one isolated worktree lease."""

    ACTIVE = "active"
    RETAINED = "retained"
    RELEASED = "released"
    STOPPED = "stopped"


class HandoffStatus(StrEnum):
    """State exposed to the next role or to an operator."""

    BLOCKED = "blocked"
    AWAITING_APPROVAL = "awaiting_approval"
    AWAITING_CLARIFICATION = "awaiting_clarification"
    ACCEPTED = "accepted"
    STOPPED = "stopped"


class RepairStatus(StrEnum):
    """Durable lifecycle state for one pull-request conflict repair."""

    RUNNING = "running"
    BLOCKED = "blocked"
    AWAITING_APPROVAL = "awaiting_approval"
    AWAITING_CLARIFICATION = "awaiting_clarification"
    NOT_REQUIRED = "not_required"
    SUCCEEDED = "succeeded"


class GitDeliveryStatus(StrEnum):
    """Result of committing and pushing one leased worktree."""

    PUSHED = "pushed"
    NO_CHANGES = "no_changes"
    BLOCKED = "blocked"


ALLOWED_ROLE_TRANSITIONS: Mapping[WorkflowRole, frozenset[WorkflowRole]] = {
    WorkflowRole.PLANNER: frozenset({WorkflowRole.WRITER}),
    WorkflowRole.WRITER: frozenset({WorkflowRole.REVIEWER, WorkflowRole.OPERATOR}),
    WorkflowRole.REVIEWER: frozenset({WorkflowRole.OPERATOR}),
    WorkflowRole.OPERATOR: frozenset(),
}

DEFAULT_LEASE_TTL_SECONDS = 300


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _required(value: str, label: str, limit: int = 400) -> str:
    normalized = " ".join(value.split())
    if not normalized:
        raise WorkflowError(f"{label} is required")
    if len(normalized) > limit:
        raise WorkflowError(f"{label} must be at most {limit} characters")
    return normalized


def _path_required(value: str, label: str, limit: int = 1_000) -> str:
    if not value.strip():
        raise WorkflowError(f"{label} is required")
    if len(value) > limit:
        raise WorkflowError(f"{label} must be at most {limit} characters")
    return value


def _optional(value: str, limit: int = 400) -> str:
    normalized = " ".join(value.split())
    if len(normalized) > limit:
        raise WorkflowError(f"Value must be at most {limit} characters")
    return normalized


def _lease_expiry(ttl_seconds: int) -> str:
    return (datetime.now(UTC) + timedelta(seconds=ttl_seconds)).isoformat()


def _lease_is_expired(expires_at: object) -> bool:
    if not isinstance(expires_at, str) or not expires_at:
        return True
    try:
        return datetime.fromisoformat(expires_at) <= datetime.now(UTC)
    except (TypeError, ValueError):
        return True


def _repository_identity(remote: str) -> str | None:
    normalized = remote.replace("\\", "/")
    match = re.search(r"([^/:\s]+/[^/\s]+?)(?:\.git)?$", normalized)
    return None if match is None else match.group(1)


@dataclass(frozen=True, slots=True)
class CheckResult:
    """Evidence produced by one deterministic check."""

    name: str
    passed: bool
    evidence: str

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "passed": self.passed,
            "evidence": self.evidence,
        }


@dataclass(frozen=True, slots=True)
class GateResult:
    """Decision and evidence for a guarded model call."""

    gate: str
    allowed: bool
    checks: tuple[CheckResult, ...]
    required_action: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "gate": self.gate,
            "allowed": self.allowed,
            "checks": [check.as_dict() for check in self.checks],
            "required_action": self.required_action,
        }


@dataclass(frozen=True, slots=True)
class WorkspaceLease:
    """A unique active agent, branch, and worktree combination."""

    lease_id: str
    agent_id: str
    branch: str
    worktree_path: str
    status: LeaseStatus
    created_at: str
    updated_at: str
    lease_token: str | None = None
    expires_at: str | None = None
    stop_reason: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "lease_id": self.lease_id,
            "agent_id": self.agent_id,
            "branch": self.branch,
            "worktree_path": self.worktree_path,
            "status": self.status.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "lease_token": self.lease_token,
            "expires_at": self.expires_at,
            "stop_reason": self.stop_reason,
        }


@dataclass(frozen=True, slots=True)
class HandoffRecord:
    """Durable handoff state, including all evidence needed to continue."""

    handoff_id: str
    lease_id: str
    source_role: WorkflowRole
    target_role: WorkflowRole
    commit_sha: str
    source_state: str
    status: HandoffStatus
    checks: tuple[CheckResult, ...]
    constitution_rules: tuple[str, ...]
    required_action: str | None
    approval_actor: str | None
    approval_note: str | None
    created_at: str
    updated_at: str

    def as_dict(self) -> dict[str, object]:
        return {
            "handoff_id": self.handoff_id,
            "lease_id": self.lease_id,
            "source_role": self.source_role.value,
            "target_role": self.target_role.value,
            "commit_sha": self.commit_sha,
            "source_state": self.source_state,
            "status": self.status.value,
            "verification_evidence": [check.as_dict() for check in self.checks],
            "constitution_rules": list(self.constitution_rules),
            "required_action": self.required_action,
            "approval_actor": self.approval_actor,
            "approval_note": self.approval_note,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class GitDeliveryResult:
    """Redacted commit and push evidence for one leased branch."""

    status: GitDeliveryStatus
    lease_id: str
    branch: str
    commit_sha: str | None
    evidence: str

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "lease_id": self.lease_id,
            "branch": self.branch,
            "commit_sha": self.commit_sha,
            "evidence": self.evidence,
        }


@dataclass(frozen=True, slots=True)
class RepairRecord:
    """Persisted conflict-repair identity, evidence, and operator state."""

    repair_id: str
    pull_request_id: str
    repository: str
    pull_request_number: int
    source_branch: str
    target_branch: str
    expected_head: str
    target_head: str
    repair_branch: str
    worktree_path: str
    lease_id: str | None
    status: RepairStatus
    repaired_head: str | None
    checks: tuple[CheckResult, ...]
    evidence: Mapping[str, object]
    required_action: str | None
    created_at: str
    updated_at: str

    def as_dict(self) -> dict[str, object]:
        return {
            "repair_id": self.repair_id,
            "pull_request_id": self.pull_request_id,
            "repository": self.repository,
            "pull_request_number": self.pull_request_number,
            "source_branch": self.source_branch,
            "target_branch": self.target_branch,
            "expected_head": self.expected_head,
            "target_head": self.target_head,
            "repair_branch": self.repair_branch,
            "worktree_path": self.worktree_path,
            "lease_id": self.lease_id,
            "status": self.status.value,
            "repaired_head": self.repaired_head,
            "verification_evidence": [check.as_dict() for check in self.checks],
            "evidence": dict(self.evidence),
            "required_action": self.required_action,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class DeterministicCheck(Protocol):
    """A repeatable repository check used before a gate can continue."""

    name: str

    def run(self, workspace: Path) -> CheckResult: ...


@dataclass(frozen=True, slots=True)
class CommandCheck:
    """Run one fixed command without a shell and capture its evidence."""

    name: str
    command: tuple[str, ...]
    timeout_seconds: float = 60.0

    def run(self, workspace: Path) -> CheckResult:
        if not self.command:
            raise WorkflowError(f"Command check {self.name} has no command")
        result = subprocess.run(
            self.command,
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
            check=False,
        )
        output = "\n".join(
            part.strip() for part in (result.stdout, result.stderr) if part.strip()
        )
        evidence = output[-4_000:] if output else f"exit code {result.returncode}"
        return CheckResult(self.name, result.returncode == 0, evidence)


class DeterministicCheckRunner:
    """Run configured checks in stable order and turn failures into evidence."""

    def __init__(self, checks: Iterable[DeterministicCheck]) -> None:
        self._checks = tuple(checks)
        names = [check.name for check in self._checks]
        if not names:
            raise WorkflowError("At least one deterministic check is required")
        if any(not name.strip() for name in names):
            raise WorkflowError("Every deterministic check needs a name")
        if len(set(names)) != len(names):
            raise WorkflowError("Deterministic check names must be unique")

    def run(self, workspace: Path) -> tuple[CheckResult, ...]:
        results: list[CheckResult] = []
        for check in self._checks:
            try:
                result = check.run(workspace)
                if result.name != check.name:
                    raise WorkflowError(
                        f"Check {check.name} returned evidence for {result.name}"
                    )
                if not result.evidence.strip():
                    results.append(
                        CheckResult(check.name, False, "Check returned no evidence")
                    )
                else:
                    results.append(result)
            except Exception as exc:
                results.append(
                    CheckResult(check.name, False, f"Check failed to run: {exc}")
                )
        return tuple(results)


@dataclass(frozen=True, slots=True)
class Constitution:
    """Machine-readable shared rules selected by workflow role."""

    version: int
    sections: Mapping[str, tuple[str, ...]]
    roles: Mapping[WorkflowRole, tuple[str, ...]]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "sections",
            MappingProxyType(
                {name: tuple(rules) for name, rules in self.sections.items()}
            ),
        )
        object.__setattr__(
            self,
            "roles",
            MappingProxyType(
                {role: tuple(sections) for role, sections in self.roles.items()}
            ),
        )

    @classmethod
    def load(cls, path: str | Path) -> Constitution:
        constitution_path = Path(path)
        try:
            decoded = json.loads(constitution_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkflowError(
                f"Cannot load constitution: {constitution_path}"
            ) from exc
        if not isinstance(decoded, dict):
            raise WorkflowError("Constitution must be a JSON object")
        data = cast(dict[str, object], decoded)
        version = data.get("version")
        if not isinstance(version, int) or version <= 0:
            raise WorkflowError("Constitution version must be a positive integer")
        sections = cls._load_sections(data.get("sections"))
        raw_roles = data.get("roles")
        if not isinstance(raw_roles, dict):
            raise WorkflowError("Constitution roles must be an object")
        roles: dict[WorkflowRole, tuple[str, ...]] = {}
        for raw_role, raw_sections in cast(dict[object, object], raw_roles).items():
            role = _enum_role(raw_role)
            names = cls._load_names(raw_sections, "role sections")
            if any(section not in sections for section in names):
                raise WorkflowError(
                    f"Constitution role {role.value} references an unknown section"
                )
            roles[role] = names
        return cls(version, sections, roles)

    @staticmethod
    def _load_sections(raw_sections: object) -> dict[str, tuple[str, ...]]:
        if not isinstance(raw_sections, dict):
            raise WorkflowError("Constitution sections must be an object")
        sections: dict[str, tuple[str, ...]] = {}
        for raw_name, raw_rules in cast(dict[object, object], raw_sections).items():
            if not isinstance(raw_name, str) or not raw_name.strip():
                raise WorkflowError("Constitution section names are required")
            sections[raw_name] = Constitution._load_names(raw_rules, "rules")
        return sections

    @staticmethod
    def _load_names(raw_names: object, label: str) -> tuple[str, ...]:
        if not isinstance(raw_names, list) or not raw_names:
            raise WorkflowError(f"Constitution {label} must be a non-empty list")
        raw_items = cast(list[object], raw_names)
        names = tuple(
            _required(item, label[:-1] if label.endswith("s") else label, 1_000)
            for item in raw_items
            if isinstance(item, str)
        )
        if len(names) != len(raw_items):
            raise WorkflowError(f"Constitution {label} must contain strings")
        return names

    def rules_for(self, role: WorkflowRole | str) -> tuple[str, ...]:
        normalized = _enum_role(role)
        sections = self.roles.get(normalized)
        if sections is None:
            raise WorkflowError(
                f"No constitution rules configured for {normalized.value}"
            )
        return tuple(rule for section in sections for rule in self.sections[section])


def _enum_role(value: object) -> WorkflowRole:
    try:
        return WorkflowRole(value)
    except ValueError as exc:
        raise WorkflowError(f"Unknown workflow role: {value}") from exc


def _enum_status(value: object) -> HandoffStatus:
    try:
        return HandoffStatus(value)
    except ValueError as exc:
        raise WorkflowError(f"Unknown handoff status: {value}") from exc


def _checks_to_json(checks: Sequence[CheckResult]) -> str:
    return json.dumps([check.as_dict() for check in checks], sort_keys=True)


def _checks_from_json(value: object) -> tuple[CheckResult, ...]:
    if not isinstance(value, str):
        raise WorkflowError("Stored verification evidence is invalid")
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise WorkflowError("Stored verification evidence is invalid") from exc
    if not isinstance(decoded, list):
        raise WorkflowError("Stored verification evidence is invalid")
    checks: list[CheckResult] = []
    for item in cast(list[object], decoded):
        if not isinstance(item, dict):
            raise WorkflowError("Stored verification evidence is invalid")
        mapping = cast(dict[str, object], item)
        name = mapping.get("name")
        passed = mapping.get("passed")
        evidence = mapping.get("evidence")
        if (
            not isinstance(name, str)
            or not isinstance(passed, bool)
            or not isinstance(evidence, str)
        ):
            raise WorkflowError("Stored verification evidence is invalid")
        checks.append(CheckResult(name, passed, evidence))
    return tuple(checks)


class WorkflowStore:
    """SQLite persistence for leases, handoffs, and gate evidence."""

    def __init__(
        self,
        database: str | Path = ":memory:",
        lease_ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
    ) -> None:
        if lease_ttl_seconds <= 0:
            raise WorkflowError("Lease TTL must be positive")
        self._lease_ttl_seconds = lease_ttl_seconds
        if database != ":memory:":
            Path(database).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            str(database), check_same_thread=False, isolation_level=None
        )
        self._connection.row_factory = sqlite3.Row
        self._lock = RLock()
        self._initialize()

    @property
    def lease_heartbeat_seconds(self) -> float:
        return max(self._lease_ttl_seconds / 3, 0.01)

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    @contextmanager
    def _transaction(
        self, *, reclaim_expired: bool = True
    ) -> Generator[sqlite3.Connection]:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                if reclaim_expired:
                    self._expire_active_leases(self._connection)
                yield self._connection
            except Exception:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()

    def _initialize(self) -> None:
        with self._lock:
            self._connection.executescript(
                """
                PRAGMA foreign_keys = ON;
                CREATE TABLE IF NOT EXISTS workflow_leases (
                    lease_id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    branch TEXT NOT NULL,
                    worktree_path TEXT NOT NULL,
                    lease_token TEXT,
                    expires_at TEXT,
                    status TEXT NOT NULL,
                    stop_reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                DROP INDEX IF EXISTS active_workflow_branch;
                DROP INDEX IF EXISTS active_workflow_worktree;
                CREATE UNIQUE INDEX active_workflow_branch
                    ON workflow_leases(branch)
                    WHERE status IN ('active', 'retained');
                CREATE UNIQUE INDEX active_workflow_worktree
                    ON workflow_leases(worktree_path)
                    WHERE status IN ('active', 'retained');
                CREATE UNIQUE INDEX IF NOT EXISTS active_dashboard_run_workspace
                    ON workflow_leases(agent_id)
                    WHERE agent_id GLOB 'dashboard-run:*'
                    AND status IN ('active', 'retained');
                CREATE TABLE IF NOT EXISTS workflow_handoffs (
                    handoff_id TEXT PRIMARY KEY,
                    lease_id TEXT NOT NULL,
                    source_role TEXT NOT NULL,
                    target_role TEXT NOT NULL,
                    commit_sha TEXT NOT NULL,
                    source_state TEXT NOT NULL,
                    status TEXT NOT NULL,
                    checks_json TEXT NOT NULL,
                    constitution_json TEXT NOT NULL,
                    required_action TEXT,
                    approval_actor TEXT,
                    approval_note TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(lease_id) REFERENCES workflow_leases(lease_id)
                );
                CREATE INDEX IF NOT EXISTS workflow_handoffs_by_lease
                    ON workflow_handoffs(lease_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS workflow_gates (
                    gate_id TEXT PRIMARY KEY,
                    lease_id TEXT NOT NULL,
                    gate TEXT NOT NULL,
                    allowed INTEGER NOT NULL,
                    checks_json TEXT NOT NULL,
                    required_action TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(lease_id) REFERENCES workflow_leases(lease_id)
                );
                CREATE TABLE IF NOT EXISTS workflow_repairs (
                    repair_id TEXT PRIMARY KEY,
                    pull_request_id TEXT NOT NULL,
                    repository TEXT NOT NULL,
                    pull_request_number INTEGER NOT NULL,
                    source_branch TEXT NOT NULL,
                    target_branch TEXT NOT NULL,
                    expected_head TEXT NOT NULL,
                    target_head TEXT NOT NULL,
                    repair_branch TEXT NOT NULL,
                    worktree_path TEXT NOT NULL,
                    lease_id TEXT,
                    status TEXT NOT NULL,
                    repaired_head TEXT,
                    checks_json TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    required_action TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS workflow_repairs_identity
                    ON workflow_repairs(pull_request_id, expected_head);
                """
            )
            lease_columns = {
                str(row["name"])
                for row in self._connection.execute(
                    "PRAGMA table_info(workflow_leases)"
                ).fetchall()
            }
            for column in ("lease_token", "expires_at"):
                if column not in lease_columns:
                    self._connection.execute(
                        f"ALTER TABLE workflow_leases ADD COLUMN {column} TEXT"
                    )
            active_rows = self._connection.execute(
                """
                SELECT lease_id, lease_token, expires_at
                FROM workflow_leases
                WHERE status = ?
                """,
                (LeaseStatus.ACTIVE.value,),
            ).fetchall()
            for row in active_rows:
                if row["lease_token"] is None or row["expires_at"] is None:
                    self._connection.execute(
                        """
                        UPDATE workflow_leases
                        SET lease_token = ?, expires_at = ?
                        WHERE lease_id = ?
                        """,
                        (
                            str(uuid4()),
                            _lease_expiry(self._lease_ttl_seconds),
                            str(row["lease_id"]),
                        ),
                    )

            self._connection.execute(
                """
                UPDATE workflow_repairs
                SET status = ?, required_action = ?, updated_at = ?
                WHERE lease_id IN (
                    SELECT lease_id FROM workflow_leases WHERE status = ?
                ) AND status = ?
                """,
                (
                    RepairStatus.AWAITING_CLARIFICATION.value,
                    "Lease expired and requires operator recovery",
                    _now(),
                    LeaseStatus.STOPPED.value,
                    RepairStatus.RUNNING.value,
                ),
            )

    @staticmethod
    def _lease_from_row(row: sqlite3.Row) -> WorkspaceLease:
        return WorkspaceLease(
            lease_id=str(row["lease_id"]),
            agent_id=str(row["agent_id"]),
            branch=str(row["branch"]),
            worktree_path=str(row["worktree_path"]),
            status=LeaseStatus(str(row["status"])),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            lease_token=row["lease_token"],
            expires_at=row["expires_at"],
            stop_reason=row["stop_reason"],
        )

    def _expire_active_leases(self, connection: sqlite3.Connection) -> tuple[str, ...]:
        rows = connection.execute(
            "SELECT * FROM workflow_leases WHERE status = ?",
            (LeaseStatus.ACTIVE.value,),
        ).fetchall()
        expired_ids: list[str] = []
        for row in rows:
            if not _lease_is_expired(row["expires_at"]):
                continue
            lease_id = str(row["lease_id"])
            timestamp = _now()
            connection.execute(
                """
                UPDATE workflow_handoffs
                SET status = ?, required_action = ?, updated_at = ?
                WHERE lease_id = ? AND status NOT IN (?, ?)
                """,
                (
                    HandoffStatus.STOPPED.value,
                    "Lease expired and requires operator recovery",
                    timestamp,
                    lease_id,
                    HandoffStatus.ACCEPTED.value,
                    HandoffStatus.STOPPED.value,
                ),
            )
            connection.execute(
                """
                UPDATE workflow_leases
                SET status = ?, stop_reason = ?, updated_at = ?
                WHERE lease_id = ? AND status = ?
                """,
                (
                    LeaseStatus.STOPPED.value,
                    "Lease expired and requires operator recovery",
                    timestamp,
                    lease_id,
                    LeaseStatus.ACTIVE.value,
                ),
            )
            connection.execute(
                """
                UPDATE workflow_repairs
                SET status = ?, required_action = ?, updated_at = ?
                WHERE lease_id = ? AND status = ?
                """,
                (
                    RepairStatus.AWAITING_CLARIFICATION.value,
                    "Lease expired and requires operator recovery",
                    timestamp,
                    lease_id,
                    RepairStatus.RUNNING.value,
                ),
            )
            expired_ids.append(lease_id)
        return tuple(expired_ids)

    @staticmethod
    def _handoff_from_row(row: sqlite3.Row) -> HandoffRecord:
        return HandoffRecord(
            handoff_id=str(row["handoff_id"]),
            lease_id=str(row["lease_id"]),
            source_role=_enum_role(row["source_role"]),
            target_role=_enum_role(row["target_role"]),
            commit_sha=str(row["commit_sha"]),
            source_state=str(row["source_state"]),
            status=_enum_status(row["status"]),
            checks=_checks_from_json(row["checks_json"]),
            constitution_rules=tuple(
                cast(list[str], json.loads(str(row["constitution_json"])))
            ),
            required_action=row["required_action"],
            approval_actor=row["approval_actor"],
            approval_note=row["approval_note"],
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _repair_from_row(row: sqlite3.Row) -> RepairRecord:
        try:
            evidence = json.loads(str(row["evidence_json"]))
        except json.JSONDecodeError as exc:
            raise WorkflowError("Stored repair evidence is invalid") from exc
        if not isinstance(evidence, dict):
            raise WorkflowError("Stored repair evidence is invalid")
        try:
            status = RepairStatus(str(row["status"]))
        except ValueError as exc:
            raise WorkflowError("Stored repair status is invalid") from exc
        return RepairRecord(
            repair_id=str(row["repair_id"]),
            pull_request_id=str(row["pull_request_id"]),
            repository=str(row["repository"]),
            pull_request_number=int(row["pull_request_number"]),
            source_branch=str(row["source_branch"]),
            target_branch=str(row["target_branch"]),
            expected_head=str(row["expected_head"]),
            target_head=str(row["target_head"]),
            repair_branch=str(row["repair_branch"]),
            worktree_path=str(row["worktree_path"]),
            lease_id=row["lease_id"],
            status=status,
            repaired_head=row["repaired_head"],
            checks=_checks_from_json(row["checks_json"]),
            evidence=cast(Mapping[str, object], evidence),
            required_action=row["required_action"],
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def begin_repair(
        self,
        repair_id: str,
        pull_request_id: str,
        repository: str,
        pull_request_number: int,
        source_branch: str,
        target_branch: str,
        expected_head: str,
        target_head: str,
        repair_branch: str,
        worktree_path: str,
        evidence: Mapping[str, object],
    ) -> RepairRecord:
        values = (
            _required(repair_id, "repair id", 200),
            _required(pull_request_id, "pull-request id", 300),
            _required(repository, "repository", 300),
            pull_request_number,
            _optional(source_branch),
            _optional(target_branch),
            _optional(expected_head, 200),
            _optional(target_head, 200),
            _required(repair_branch, "repair branch", 400),
            _path_required(worktree_path, "repair worktree", 1_000),
            RepairStatus.RUNNING.value,
            _checks_to_json(()),
            json.dumps(dict(evidence), sort_keys=True),
            _now(),
            _now(),
        )
        if pull_request_number <= 0:
            raise WorkflowError("Pull-request number must be positive")
        with self._transaction() as connection:
            existing = connection.execute(
                """
                SELECT * FROM workflow_repairs
                WHERE pull_request_id = ? AND expected_head = ?
                """,
                (values[1], values[6]),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO workflow_repairs(
                        repair_id, pull_request_id, repository, pull_request_number,
                        source_branch, target_branch, expected_head, target_head,
                        repair_branch, worktree_path, lease_id, status, repaired_head,
                        checks_json, evidence_json, required_action, created_at,
                        updated_at
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL, ?, ?, NULL,
                        ?, ?
                    )
                    """,
                    values,
                )
            else:
                return self._repair_from_row(existing)
            row = connection.execute(
                "SELECT * FROM workflow_repairs WHERE repair_id = ?",
                (values[0],),
            ).fetchone()
        if row is None:
            raise WorkflowError("Repair record was not persisted")
        return self._repair_from_row(row)

    def attach_repair_workspace(
        self, repair_id: str, lease_id: str, worktree_path: str
    ) -> RepairRecord:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM workflow_repairs WHERE repair_id = ?", (repair_id,)
            ).fetchone()
            if row is None:
                raise WorkflowError(f"Unknown repair: {repair_id}")
            current_lease = row["lease_id"]
            if current_lease is not None and current_lease != lease_id:
                raise WorkflowError("Repair already has a different workspace lease")
            connection.execute(
                """
                UPDATE workflow_repairs
                SET lease_id = ?, worktree_path = ?, updated_at = ?
                WHERE repair_id = ? AND status = ?
                """,
                (
                    lease_id,
                    _path_required(worktree_path, "repair worktree", 1_000),
                    _now(),
                    repair_id,
                    RepairStatus.RUNNING.value,
                ),
            )
        record = self.get_repair(repair_id)
        if record is None:
            raise WorkflowError("Repair record disappeared")
        return record

    def finish_repair(
        self,
        repair_id: str,
        status: RepairStatus,
        required_action: str | None,
        evidence: Mapping[str, object],
        checks: Sequence[CheckResult] = (),
        repaired_head: str | None = None,
    ) -> RepairRecord:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM workflow_repairs WHERE repair_id = ?", (repair_id,)
            ).fetchone()
            if row is None:
                raise WorkflowError(f"Unknown repair: {repair_id}")
            current = RepairStatus(str(row["status"]))
            if current is not RepairStatus.RUNNING:
                return self._repair_from_row(row)
            connection.execute(
                """
                UPDATE workflow_repairs
                SET status = ?, repaired_head = ?, checks_json = ?, evidence_json = ?,
                    required_action = ?, updated_at = ?
                WHERE repair_id = ? AND status = ?
                """,
                (
                    status.value,
                    repaired_head,
                    _checks_to_json(checks),
                    json.dumps(dict(evidence), sort_keys=True),
                    required_action,
                    _now(),
                    repair_id,
                    RepairStatus.RUNNING.value,
                ),
            )
        record = self.get_repair(repair_id)
        if record is None:
            raise WorkflowError("Repair record disappeared")
        return record

    def get_repair(self, repair_id: str) -> RepairRecord | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM workflow_repairs WHERE repair_id = ?", (repair_id,)
            ).fetchone()
        return None if row is None else self._repair_from_row(row)

    def get_repair_for_identity(
        self, pull_request_id: str, expected_head: str
    ) -> RepairRecord | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT * FROM workflow_repairs
                WHERE pull_request_id = ? AND expected_head = ?
                """,
                (pull_request_id, expected_head),
            ).fetchone()
        return None if row is None else self._repair_from_row(row)

    def get_lease(self, lease_id: str) -> WorkspaceLease | None:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM workflow_leases WHERE lease_id = ?", (lease_id,)
            ).fetchone()
        return None if row is None else self._lease_from_row(row)

    def get_lease_for_agent(self, agent_id: str) -> WorkspaceLease | None:
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM workflow_leases
                WHERE agent_id = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (agent_id,),
            ).fetchone()
        return None if row is None else self._lease_from_row(row)

    def dashboard_run_leases(self) -> tuple[WorkspaceLease, ...]:
        with self._transaction() as connection:
            rows = connection.execute(
                """
                SELECT * FROM workflow_leases
                WHERE agent_id GLOB 'dashboard-run:*'
                AND status IN (?, ?)
                ORDER BY created_at
                """,
                (LeaseStatus.ACTIVE.value, LeaseStatus.STOPPED.value),
            ).fetchall()
        return tuple(self._lease_from_row(row) for row in rows)

    def reclaim_expired(self) -> tuple[WorkspaceLease, ...]:
        with self._transaction(reclaim_expired=False) as connection:
            expired_ids = self._expire_active_leases(connection)
            if not expired_ids:
                return ()
            placeholders = ", ".join("?" for _ in expired_ids)
            rows = connection.execute(
                f"SELECT * FROM workflow_leases WHERE lease_id IN ({placeholders})",
                expired_ids,
            ).fetchall()
            return tuple(self._lease_from_row(row) for row in rows)

    def require_lease_token(
        self,
        lease_id: str,
        lease_token: str | None,
        *,
        allow_stopped: bool = False,
    ) -> WorkspaceLease:
        lease = self.get_lease(lease_id)
        if lease is None:
            raise WorkflowError(f"Unknown workspace lease: {lease_id}")
        if lease.status is not LeaseStatus.ACTIVE and not (
            allow_stopped and lease.status is LeaseStatus.STOPPED
        ):
            raise WorkflowError(f"Workspace lease is {lease.status.value}")
        if not lease_token or lease.lease_token != lease_token:
            raise WorkflowError("Lease token is invalid")
        return lease

    def ensure_release_allowed(self, lease_id: str) -> WorkspaceLease:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM workflow_leases WHERE lease_id = ?", (lease_id,)
            ).fetchone()
            if row is None:
                raise WorkflowError(f"Unknown workspace lease: {lease_id}")
            if LeaseStatus(str(row["status"])) is not LeaseStatus.ACTIVE:
                raise WorkflowError(f"Workspace lease is {row['status']}")
            pending = connection.execute(
                """
                SELECT 1 FROM workflow_handoffs
                WHERE lease_id = ? AND status IN (?, ?)
                LIMIT 1
                """,
                (
                    lease_id,
                    HandoffStatus.AWAITING_APPROVAL.value,
                    HandoffStatus.AWAITING_CLARIFICATION.value,
                ),
            ).fetchone()
            if pending is not None:
                raise WorkflowError("Cannot release a lease with an unresolved handoff")
        lease = self.get_lease(lease_id)
        if lease is None:
            raise WorkflowError("Workspace lease disappeared")
        return lease

    def renew_lease(self, lease_id: str, lease_token: str | None) -> WorkspaceLease:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM workflow_leases WHERE lease_id = ?", (lease_id,)
            ).fetchone()
            if row is None:
                raise WorkflowError(f"Unknown workspace lease: {lease_id}")
            if LeaseStatus(str(row["status"])) is not LeaseStatus.ACTIVE:
                raise WorkflowError(f"Workspace lease is {row['status']}")
            if not lease_token or row["lease_token"] != lease_token:
                raise WorkflowError("Lease token is invalid")
            connection.execute(
                """
                UPDATE workflow_leases
                SET expires_at = ?, updated_at = ?
                WHERE lease_id = ? AND status = ? AND lease_token = ?
                """,
                (
                    _lease_expiry(self._lease_ttl_seconds),
                    _now(),
                    lease_id,
                    LeaseStatus.ACTIVE.value,
                    lease_token,
                ),
            )
        lease = self.get_lease(lease_id)
        if lease is None:
            raise WorkflowError("Workspace lease disappeared")
        return lease

    def retain_lease(self, lease_id: str, lease_token: str | None) -> WorkspaceLease:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM workflow_leases WHERE lease_id = ?", (lease_id,)
            ).fetchone()
            if row is None:
                raise WorkflowError(f"Unknown workspace lease: {lease_id}")
            if LeaseStatus(str(row["status"])) is not LeaseStatus.ACTIVE:
                raise WorkflowError(f"Workspace lease is {row['status']}")
            if not lease_token or row["lease_token"] != lease_token:
                raise WorkflowError("Lease token is invalid")
            connection.execute(
                """
                UPDATE workflow_leases
                SET status = ?, expires_at = NULL, updated_at = ?
                WHERE lease_id = ? AND status = ? AND lease_token = ?
                """,
                (
                    LeaseStatus.RETAINED.value,
                    _now(),
                    lease_id,
                    LeaseStatus.ACTIVE.value,
                    lease_token,
                ),
            )
        lease = self.get_lease(lease_id)
        if lease is None:
            raise WorkflowError("Workspace lease disappeared")
        return lease

    def acquire_lease(
        self, agent_id: str, branch: str, worktree_path: str
    ) -> WorkspaceLease:
        agent_id = _required(agent_id, "agent id")
        branch = _required(branch, "branch")
        worktree_path = _path_required(worktree_path, "worktree path")
        timestamp = _now()
        lease_id = str(uuid4())
        lease_token = str(uuid4())
        try:
            with self._transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO workflow_leases(
                        lease_id, agent_id, branch, worktree_path, lease_token,
                        expires_at, status, stop_reason, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                    """,
                    (
                        lease_id,
                        agent_id,
                        branch,
                        worktree_path,
                        lease_token,
                        _lease_expiry(self._lease_ttl_seconds),
                        LeaseStatus.ACTIVE.value,
                        timestamp,
                        timestamp,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise WorkflowError(
                "An active agent already owns this branch or worktree"
            ) from exc
        lease = self.get_lease(lease_id)
        if lease is None:
            raise WorkflowError("Workspace lease was not persisted")
        return lease

    def release_lease(
        self, lease_id: str, *, allow_stopped: bool = False
    ) -> WorkspaceLease:
        return self._set_lease_status(
            lease_id, LeaseStatus.RELEASED, None, allow_stopped=allow_stopped
        )

    def stop_lease(self, lease_id: str, reason: str) -> WorkspaceLease:
        return self._set_lease_status(
            lease_id, LeaseStatus.STOPPED, _required(reason, "stop reason")
        )

    def _set_lease_status(
        self,
        lease_id: str,
        status: LeaseStatus,
        reason: str | None,
        *,
        allow_stopped: bool = False,
    ) -> WorkspaceLease:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM workflow_leases WHERE lease_id = ?", (lease_id,)
            ).fetchone()
            if row is None:
                raise WorkflowError(f"Unknown workspace lease: {lease_id}")
            current_status = LeaseStatus(str(row["status"]))
            if current_status is LeaseStatus.RELEASED:
                return self._lease_from_row(row)
            if current_status is LeaseStatus.STOPPED:
                if not allow_stopped:
                    return self._lease_from_row(row)
            elif current_status is LeaseStatus.RETAINED:
                if status not in {LeaseStatus.RELEASED, LeaseStatus.STOPPED}:
                    raise WorkflowError("A retained workspace cannot be renewed")
            else:
                assert current_status is LeaseStatus.ACTIVE
            if status is LeaseStatus.RELEASED:
                pending = connection.execute(
                    """
                    SELECT 1 FROM workflow_handoffs
                    WHERE lease_id = ? AND status IN (?, ?)
                    LIMIT 1
                    """,
                    (
                        lease_id,
                        HandoffStatus.AWAITING_APPROVAL.value,
                        HandoffStatus.AWAITING_CLARIFICATION.value,
                    ),
                ).fetchone()
                if pending is not None:
                    raise WorkflowError(
                        "Cannot release a lease with an unresolved handoff"
                    )
            timestamp = _now()
            if status is LeaseStatus.STOPPED:
                connection.execute(
                    """
                    UPDATE workflow_handoffs
                    SET status = ?, required_action = ?, updated_at = ?
                    WHERE lease_id = ? AND status NOT IN (?, ?)
                    """,
                    (
                        HandoffStatus.STOPPED.value,
                        reason,
                        timestamp,
                        lease_id,
                        HandoffStatus.ACCEPTED.value,
                        HandoffStatus.STOPPED.value,
                    ),
                )
            connection.execute(
                """
                UPDATE workflow_leases
                SET status = ?, stop_reason = ?, updated_at = ?
                WHERE lease_id = ?
                """,
                (status.value, reason, timestamp, lease_id),
            )
        lease = self.get_lease(lease_id)
        if lease is None:
            raise WorkflowError("Workspace lease disappeared")
        return lease

    def record_handoff(
        self,
        lease_id: str,
        source_role: WorkflowRole,
        target_role: WorkflowRole,
        commit_sha: str,
        source_state: str,
        status: HandoffStatus,
        checks: Sequence[CheckResult],
        constitution_rules: Sequence[str],
        required_action: str | None,
    ) -> HandoffRecord:
        handoff_id = str(uuid4())
        timestamp = _now()
        with self._transaction() as connection:
            lease = connection.execute(
                "SELECT lease_id, status FROM workflow_leases WHERE lease_id = ?",
                (lease_id,),
            ).fetchone()
            if lease is None:
                raise WorkflowError(f"Unknown workspace lease: {lease_id}")
            if LeaseStatus(str(lease["status"])) is not LeaseStatus.ACTIVE:
                raise WorkflowError(f"Workspace lease is {str(lease['status'])}")
            open_handoff = connection.execute(
                """
                SELECT 1 FROM workflow_handoffs
                WHERE lease_id = ? AND status IN (?, ?)
                LIMIT 1
                """,
                (
                    lease_id,
                    HandoffStatus.AWAITING_APPROVAL.value,
                    HandoffStatus.AWAITING_CLARIFICATION.value,
                ),
            ).fetchone()
            if open_handoff is not None:
                raise WorkflowError("An unresolved handoff already exists")
            connection.execute(
                """
                INSERT INTO workflow_handoffs(
                    handoff_id, lease_id, source_role, target_role, commit_sha,
                    source_state, status, checks_json, constitution_json,
                    required_action, approval_actor, approval_note,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)
                """,
                (
                    handoff_id,
                    lease_id,
                    source_role.value,
                    target_role.value,
                    commit_sha,
                    source_state,
                    status.value,
                    _checks_to_json(checks),
                    json.dumps(list(constitution_rules)),
                    required_action,
                    timestamp,
                    timestamp,
                ),
            )
        handoff = self.get_handoff(handoff_id)
        if handoff is None:
            raise WorkflowError("Handoff was not persisted")
        return handoff

    def get_handoff(self, handoff_id: str) -> HandoffRecord | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM workflow_handoffs WHERE handoff_id = ?",
                (handoff_id,),
            ).fetchone()
        return None if row is None else self._handoff_from_row(row)

    def latest_handoff(self, lease_id: str) -> HandoffRecord | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT * FROM workflow_handoffs
                WHERE lease_id = ?
                ORDER BY created_at DESC, handoff_id DESC
                LIMIT 1
                """,
                (lease_id,),
            ).fetchone()
        return None if row is None else self._handoff_from_row(row)

    def update_handoff(
        self,
        handoff_id: str,
        status: HandoffStatus,
        required_action: str | None,
        checks: Sequence[CheckResult] | None = None,
        approval_actor: str | None = None,
        approval_note: str | None = None,
        expected_status: HandoffStatus | None = None,
    ) -> HandoffRecord:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM workflow_handoffs WHERE handoff_id = ?",
                (handoff_id,),
            ).fetchone()
            if row is None:
                raise WorkflowError(f"Unknown handoff: {handoff_id}")
            where = "WHERE handoff_id = ?"
            parameters: list[object] = [handoff_id]
            if expected_status is not None:
                where += " AND status = ?"
                parameters.append(expected_status.value)
            parameters = [
                status.value,
                required_action,
                _checks_to_json(checks)
                if checks is not None
                else str(row["checks_json"]),
                approval_actor if approval_actor is not None else row["approval_actor"],
                approval_note if approval_note is not None else row["approval_note"],
                _now(),
                *parameters,
            ]
            updated = connection.execute(
                """
                UPDATE workflow_handoffs
                SET status = ?, required_action = ?, checks_json = ?,
                    approval_actor = ?, approval_note = ?, updated_at = ?
                """
                + where,
                parameters,
            )
            if updated.rowcount != 1:
                raise WorkflowError("Handoff transition conflict")
        handoff = self.get_handoff(handoff_id)
        if handoff is None:
            raise WorkflowError("Handoff disappeared")
        return handoff

    def record_gate(self, lease_id: str, result: GateResult) -> None:
        with self._transaction() as connection:
            lease = connection.execute(
                "SELECT lease_id FROM workflow_leases WHERE lease_id = ?",
                (lease_id,),
            ).fetchone()
            if lease is None:
                raise WorkflowError(f"Unknown workspace lease: {lease_id}")
            connection.execute(
                """
                INSERT INTO workflow_gates(
                    gate_id, lease_id, gate, allowed, checks_json,
                    required_action, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid4()),
                    lease_id,
                    result.gate,
                    int(result.allowed),
                    _checks_to_json(result.checks),
                    result.required_action,
                    _now(),
                ),
            )

    def latest_gate(self, lease_id: str, gate: str) -> GateResult | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT gate, allowed, checks_json, required_action
                FROM workflow_gates
                WHERE lease_id = ? AND gate = ?
                ORDER BY created_at DESC, gate_id DESC
                LIMIT 1
                """,
                (lease_id, gate),
            ).fetchone()
        if row is None:
            return None
        return GateResult(
            str(row["gate"]),
            bool(row["allowed"]),
            _checks_from_json(str(row["checks_json"])),
            None if row["required_action"] is None else str(row["required_action"]),
        )


@dataclass(frozen=True, slots=True)
class MergeResult:
    """Result of integrating the exact target commit into a repair worktree."""

    conflicted: bool
    evidence: str


class GitWorktreeManager:
    """Create and remove real git worktrees behind durable lease claims."""

    def __init__(
        self,
        repository: str | Path,
        store: WorkflowStore,
        git_timeout_seconds: float = 60.0,
    ) -> None:
        self.repository = Path(repository).resolve()
        self.store = store
        if git_timeout_seconds <= 0:
            raise WorkflowError("Git timeout must be positive")
        self.git_timeout_seconds = git_timeout_seconds
        if not self.repository.exists():
            raise WorkflowError(f"Repository does not exist: {self.repository}")
        self.host_git_identity = (
            self._config_value("user.name"),
            self._config_value("user.email"),
        )
        self.origin_push_urls = self._origin_urls()

    def _config_value(self, key: str) -> str | None:
        result = self._run_git("config", "--get", key)
        value = result.stdout.strip()
        return value if result.returncode == 0 and value else None

    def _origin_urls(self) -> tuple[str, ...]:
        result = self._run_git("remote", "get-url", "--push", "--all", "origin")
        if result.returncode != 0:
            return ()
        return tuple(
            line.strip() for line in result.stdout.splitlines() if line.strip()
        )

    def acquire(
        self, agent_id: str, branch: str, worktree: str | Path, base_ref: str = "HEAD"
    ) -> WorkspaceLease:
        branch = _required(branch, "branch")
        base_ref = _required(base_ref, "base ref")
        path = Path(worktree).resolve()
        for stale_lease in self.store.reclaim_expired():
            self._cleanup_stopped_worktree(stale_lease.worktree_path)
            self._delete_reclaimed_branch(stale_lease.branch)
        lease = self.store.acquire_lease(agent_id, branch, str(path))
        try:
            self._git("worktree", "add", "-b", branch, str(path), base_ref)
        except Exception:
            try:
                self._cleanup_stopped_worktree(str(path))
            except WorkflowError:
                pass
            finally:
                self.store.release_lease(lease.lease_id)
            raise
        return lease

    def release(self, lease_id: str) -> WorkspaceLease:
        lease = self.store.get_lease(lease_id)
        if lease is None:
            raise WorkflowError(f"Unknown workspace lease: {lease_id}")
        if lease.status is LeaseStatus.ACTIVE:
            self.store.ensure_release_allowed(lease_id)
            self._git("worktree", "remove", lease.worktree_path)
            return self.store.release_lease(lease_id)
        if lease.status is LeaseStatus.RETAINED:
            if not self.clean(lease.worktree_path):
                raise WorkflowError(
                    "Cannot release a retained workspace with uncommitted changes"
                )
            self._git("worktree", "remove", lease.worktree_path)
            return self.store.release_lease(lease_id)
        if lease.status is LeaseStatus.STOPPED:
            self._cleanup_stopped_worktree(lease.worktree_path)
            self._delete_reclaimed_branch(lease.branch)
            return self.store.release_lease(lease_id, allow_stopped=True)
        return lease

    def head(self, worktree: str | Path) -> str:
        return self._git("-C", str(Path(worktree)), "rev-parse", "HEAD")

    def clean(self, worktree: str | Path) -> bool:
        return not self._git("-C", str(Path(worktree)), "status", "--porcelain")

    def fetch_exact_branch(
        self, branch: str, expected_head: str, destination_ref: str
    ) -> str:
        """Fetch one branch into an internal ref and prove its exact head."""

        branch = _required(branch, "remote branch")
        expected_head = _required(expected_head, "expected branch head", 200)
        destination_ref = _required(destination_ref, "destination ref", 400)
        remotes = self._run_git("remote")
        if remotes.returncode != 0:
            message = (remotes.stderr or remotes.stdout).strip()
            raise WorkflowError(message or "Could not inspect repository remotes")
        remote_exists = "origin" in {
            line.strip() for line in remotes.stdout.splitlines()
        }
        if remote_exists:
            result = self._run_git(
                "fetch",
                "--no-tags",
                "origin",
                f"refs/heads/{branch}:{destination_ref}",
            )
            if result.returncode != 0:
                message = (result.stderr or result.stdout).strip()
                raise WorkflowError(
                    message or "Could not fetch the pull-request branch"
                )
        else:
            if not self._commit_exists(expected_head):
                raise WorkflowError("Expected branch head is not available locally")
            self._git("update-ref", destination_ref, expected_head)
        actual = self._git("rev-parse", destination_ref)
        if actual != expected_head:
            raise WorkflowError("Remote branch head changed before repair started")
        return destination_ref

    def contains_commit(self, worktree: str | Path, commit: str) -> bool:
        result = self._run_git(
            "-C",
            str(Path(worktree)),
            "merge-base",
            "--is-ancestor",
            _required(commit, "commit", 200),
            "HEAD",
        )
        if result.returncode == 0:
            return True
        if result.returncode == 1:
            return False
        message = (result.stderr or result.stdout).strip()
        raise WorkflowError(message or "Could not verify commit ancestry")

    def remove_ref(self, ref: str) -> None:
        try:
            self._git("update-ref", "-d", _required(ref, "internal ref", 400))
        except WorkflowError:
            return

    def integrate_target(
        self, worktree: str | Path, expected_source_head: str, target_head: str
    ) -> MergeResult:
        workspace = Path(worktree)
        actual_source_head = self.head(workspace)
        if actual_source_head != expected_source_head:
            raise WorkflowError(
                "Repair worktree was not created from the expected head"
            )
        result = self._run_git(
            "-C",
            str(workspace),
            "merge",
            "--no-edit",
            "--no-ff",
            "--no-commit",
            target_head,
        )
        evidence = "\n".join(
            part.strip() for part in (result.stdout, result.stderr) if part.strip()
        )[-4_000:]
        if result.returncode == 0:
            return MergeResult(False, evidence or "Target branch staged for merge")
        conflicts = self._run_git(
            "-C",
            str(workspace),
            "diff",
            "--name-only",
            "--diff-filter=U",
        )
        if conflicts.returncode == 0 and conflicts.stdout.strip():
            return MergeResult(
                True,
                f"Merge conflicts: {conflicts.stdout.strip()}"[-4_000:],
            )
        raise WorkflowError(evidence or "Target branch integration failed")

    def _git(self, *arguments: str) -> str:
        result = self._run_git(*arguments)
        if result.returncode != 0:
            message = (result.stderr or result.stdout).strip()
            raise WorkflowError(message or f"git command failed: {' '.join(arguments)}")
        return result.stdout.strip()

    def run_git(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        """Run one bounded, no-shell Git command for WorkflowService."""

        return self._run_git(*arguments)

    def _run_git(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                ("git", *arguments),
                cwd=self.repository,
                capture_output=True,
                text=True,
                timeout=self.git_timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise WorkflowError(
                f"git command timed out after {self.git_timeout_seconds:g} seconds"
            ) from exc
        return result

    def _commit_exists(self, commit: str) -> bool:
        result = self._run_git("cat-file", "-t", commit)
        return result.returncode == 0 and result.stdout.strip() == "commit"

    def _cleanup_stopped_worktree(self, worktree_path: str) -> None:
        path = Path(worktree_path)
        if not path.exists():
            self._git("worktree", "prune")
            return
        self._git("worktree", "remove", "--force", str(path))

    def _delete_reclaimed_branch(self, branch: str) -> None:
        if not self._git("branch", "--list", branch):
            return
        current = self._git("branch", "--show-current")
        if current == branch:
            raise WorkflowError(f"Cannot reclaim the checked-out branch: {branch}")
        self._git("branch", "-D", branch)


class WorkflowService:
    """Enforce isolated work, committed evidence, checks, and control gates."""

    def __init__(
        self,
        store: WorkflowStore,
        repository: str | Path,
        constitution: Constitution,
        checks: Iterable[DeterministicCheck],
        worktrees: GitWorktreeManager | None = None,
    ) -> None:
        self.store = store
        self.constitution = constitution
        self.checks = DeterministicCheckRunner(checks)
        self.worktrees = worktrees or GitWorktreeManager(repository, store)

    def acquire_workspace(
        self, agent_id: str, branch: str, worktree: str | Path, base_ref: str = "HEAD"
    ) -> WorkspaceLease:
        return self.worktrees.acquire(agent_id, branch, worktree, base_ref)

    def retain_workspace(
        self, lease_id: str, lease_token: str | None
    ) -> WorkspaceLease:
        return self.store.retain_lease(lease_id, lease_token)

    def commit_and_push(
        self,
        lease_id: str,
        lease_token: str | None,
        expected_agent_id: str,
        expected_repository: str,
        commit_message: str,
        validate_run: Callable[[], None],
    ) -> GitDeliveryResult:
        lease = self.store.get_lease(lease_id)
        if lease is None:
            raise WorkflowError(f"Unknown workspace lease: {lease_id}")
        if lease.status not in {LeaseStatus.ACTIVE, LeaseStatus.RETAINED}:
            raise WorkflowError(f"Workspace lease is {lease.status.value}")
        if not lease_token or lease.lease_token != lease_token:
            raise WorkflowError("Lease token is invalid")
        if lease.agent_id != expected_agent_id:
            raise WorkflowError("Workspace lease does not belong to this run")
        try:
            normalized_message = _required(commit_message, "commit message", 200)
        except WorkflowError:
            normalized_message = ""

        def validate_pair() -> None:
            current = self.store.get_lease(lease_id)
            if current is None or current.status not in {
                LeaseStatus.ACTIVE,
                LeaseStatus.RETAINED,
            }:
                state = "missing" if current is None else current.status.value
                raise WorkflowError(f"Workspace lease is {state}")
            if current.lease_token != lease_token:
                raise WorkflowError("Lease token is invalid")
            if current.agent_id != expected_agent_id:
                raise WorkflowError("Workspace lease does not belong to this run")
            validate_run()

        def git_text(*arguments: str) -> str | None:
            try:
                result = self.worktrees.run_git(*arguments)
            except (OSError, WorkflowError):
                return None
            return result.stdout.strip() if result.returncode == 0 else None

        def target_problem(*, validate_remote: bool) -> str | None:
            workspace = Path(lease.worktree_path).resolve()
            if workspace == self.worktrees.repository or not workspace.is_dir():
                return "The leased worktree path is unavailable or not isolated."
            prefix = git_text("-C", str(workspace), "rev-parse", "--show-prefix")
            if prefix is None:
                return "The leased worktree could not be opened as a Git worktree."
            if prefix:
                return "The exact leased worktree could not be verified."
            root_common = git_text(
                "rev-parse", "--path-format=absolute", "--git-common-dir"
            )
            worktree_common = git_text(
                "-C",
                str(workspace),
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
            )
            if root_common is None or worktree_common is None:
                return "The leased worktree repository could not be verified."
            if (
                root_common.replace("\\", "/").casefold()
                != worktree_common.replace("\\", "/").casefold()
            ):
                return "The leased worktree belongs to a different repository."
            branch = git_text("-C", str(workspace), "branch", "--show-current")
            if not branch:
                return "Detached HEAD is not allowed for delivery."
            if branch != lease.branch:
                return "The current branch does not match the workspace lease."
            if validate_remote:
                remote_result = self.worktrees.run_git(
                    "-C",
                    str(workspace),
                    "remote",
                    "get-url",
                    "--push",
                    "--all",
                    "origin",
                )
                remote_urls = (
                    tuple(
                        line.strip()
                        for line in remote_result.stdout.splitlines()
                        if line.strip()
                    )
                    if remote_result.returncode == 0
                    else ()
                )
                if (
                    len(remote_urls) != 1
                    or remote_urls != self.worktrees.origin_push_urls
                    or (_repository_identity(remote_urls[0]) or "").casefold()
                    != expected_repository.casefold()
                ):
                    return (
                        "The configured push remote does not match the leased "
                        "repository."
                    )
            return None

        def record(
            status: GitDeliveryStatus,
            delivery_state: str,
            commit_sha: str | None,
            evidence: str,
        ) -> GitDeliveryResult:
            validate_pair()
            checks = (
                CheckResult(
                    "delivery_status",
                    status is not GitDeliveryStatus.BLOCKED,
                    delivery_state,
                ),
                CheckResult("commit_sha", commit_sha is not None, commit_sha or ""),
                CheckResult("branch", True, lease.branch),
                CheckResult(
                    "evidence", status is not GitDeliveryStatus.BLOCKED, evidence
                ),
            )
            self.store.record_gate(
                lease_id,
                GateResult(
                    "git_delivery",
                    status is not GitDeliveryStatus.BLOCKED,
                    checks,
                    None
                    if status is not GitDeliveryStatus.BLOCKED
                    else "Retry commit and push after resolving the delivery blocker",
                ),
            )
            return GitDeliveryResult(
                status, lease_id, lease.branch, commit_sha, evidence
            )

        validate_pair()
        problem = target_problem(validate_remote=False)
        if problem:
            return record(GitDeliveryStatus.BLOCKED, "blocked", None, problem)
        status_result = self.worktrees.run_git(
            "-C", lease.worktree_path, "status", "--porcelain", "--untracked-files=all"
        )
        if status_result.returncode != 0:
            return record(
                GitDeliveryStatus.BLOCKED,
                "blocked",
                None,
                "The leased worktree status could not be verified.",
            )
        dirty = bool(status_result.stdout.strip())
        head = git_text("-C", lease.worktree_path, "rev-parse", "HEAD")
        if head is None:
            return record(
                GitDeliveryStatus.BLOCKED,
                "blocked",
                None,
                "The leased worktree HEAD could not be verified.",
            )
        previous = self.store.latest_gate(lease_id, "git_delivery")
        previous_checks = (
            {check.name: check.evidence for check in previous.checks}
            if previous is not None
            else {}
        )
        previous_state = previous_checks.get("delivery_status")
        previous_sha = previous_checks.get("commit_sha") or None
        if previous_state == GitDeliveryStatus.PUSHED.value and previous_sha:
            return GitDeliveryResult(
                GitDeliveryStatus.PUSHED,
                lease_id,
                lease.branch,
                previous_sha,
                "The recorded commit was already pushed.",
            )
        if previous_state == GitDeliveryStatus.NO_CHANGES.value and not dirty:
            return GitDeliveryResult(
                GitDeliveryStatus.NO_CHANGES,
                lease_id,
                lease.branch,
                None,
                "The working tree was clean; no commit or push was needed.",
            )
        retry_commit = previous_state in {
            "push_pending",
            "push_failed",
            "blocked",
        } and bool(previous_sha)
        if not dirty and not retry_commit:
            return record(
                GitDeliveryStatus.NO_CHANGES,
                GitDeliveryStatus.NO_CHANGES.value,
                None,
                "The working tree was clean; no commit or push was needed.",
            )
        if retry_commit and (dirty or head != previous_sha):
            return record(
                GitDeliveryStatus.BLOCKED,
                "blocked",
                previous_sha,
                "The saved commit no longer matches the clean leased worktree.",
            )
        problem = target_problem(validate_remote=True)
        if problem:
            return record(GitDeliveryStatus.BLOCKED, "blocked", previous_sha, problem)

        commit_sha = previous_sha if retry_commit else None
        if not retry_commit:
            name, email = self.worktrees.host_git_identity
            if not name or not email:
                return record(
                    GitDeliveryStatus.BLOCKED,
                    "blocked",
                    None,
                    "Host Git user.name and user.email are required before commit.",
                )
            if not normalized_message or any(
                ord(character) < 32 for character in normalized_message
            ):
                return record(
                    GitDeliveryStatus.BLOCKED,
                    "blocked",
                    None,
                    "A valid commit message is required before staging.",
                )
            validate_pair()
            problem = target_problem(validate_remote=True)
            if problem:
                return record(GitDeliveryStatus.BLOCKED, "blocked", None, problem)
            staged = self.worktrees.run_git(
                "-C", lease.worktree_path, "add", "-A", "--", "."
            )
            if staged.returncode != 0:
                return record(
                    GitDeliveryStatus.BLOCKED,
                    "blocked",
                    None,
                    "Changes could not be staged in the leased worktree.",
                )
            diff = self.worktrees.run_git(
                "-C", lease.worktree_path, "diff", "--cached", "--quiet"
            )
            if diff.returncode == 0:
                return record(
                    GitDeliveryStatus.NO_CHANGES,
                    GitDeliveryStatus.NO_CHANGES.value,
                    None,
                    "The working tree had no committable changes.",
                )
            if diff.returncode != 1:
                return record(
                    GitDeliveryStatus.BLOCKED,
                    "blocked",
                    None,
                    "Staged changes could not be verified.",
                )
            validate_pair()
            problem = target_problem(validate_remote=True)
            if problem:
                return record(GitDeliveryStatus.BLOCKED, "blocked", None, problem)
            committed = self.worktrees.run_git(
                "-c",
                f"user.name={name}",
                "-c",
                f"user.email={email}",
                "-C",
                lease.worktree_path,
                "commit",
                "-m",
                normalized_message,
            )
            if committed.returncode != 0:
                return record(
                    GitDeliveryStatus.BLOCKED,
                    "blocked",
                    None,
                    "Git could not create the commit; staged work remains recoverable.",
                )
            commit_sha = git_text("-C", lease.worktree_path, "rev-parse", "HEAD")
            if commit_sha is None:
                return record(
                    GitDeliveryStatus.BLOCKED,
                    "blocked",
                    None,
                    "The new commit could not be verified; local work remains "
                    "recoverable.",
                )
            clean_result = self.worktrees.run_git(
                "-C", lease.worktree_path, "status", "--porcelain"
            )
            if clean_result.returncode != 0 or clean_result.stdout.strip():
                return record(
                    GitDeliveryStatus.BLOCKED,
                    "blocked",
                    commit_sha,
                    "The commit exists, but the worktree is not clean; push was "
                    "skipped.",
                )
            record(
                GitDeliveryStatus.BLOCKED,
                "push_pending",
                commit_sha,
                "The local commit is recorded and ready to push.",
            )

        validate_pair()
        problem = target_problem(validate_remote=True)
        if problem:
            return record(GitDeliveryStatus.BLOCKED, "blocked", commit_sha, problem)
        current_head = git_text("-C", lease.worktree_path, "rev-parse", "HEAD")
        current_status = self.worktrees.run_git(
            "-C", lease.worktree_path, "status", "--porcelain"
        )
        if (
            current_head != commit_sha
            or current_status.returncode != 0
            or current_status.stdout.strip()
        ):
            return record(
                GitDeliveryStatus.BLOCKED,
                "blocked",
                commit_sha,
                "Push requires the recorded commit and a clean leased worktree.",
            )
        push_url = self.worktrees.origin_push_urls[0]
        pushed = self.worktrees.run_git(
            "-C",
            lease.worktree_path,
            "push",
            "--porcelain",
            push_url,
            f"{commit_sha}:refs/heads/{lease.branch}",
        )
        if pushed.returncode != 0:
            return record(
                GitDeliveryStatus.BLOCKED,
                "push_failed",
                commit_sha,
                "Push failed; the local commit is preserved and can be retried.",
            )
        validate_pair()
        problem = target_problem(validate_remote=True)
        if problem:
            return record(GitDeliveryStatus.BLOCKED, "push_failed", commit_sha, problem)
        remote_head = self.worktrees.run_git(
            "-C",
            lease.worktree_path,
            "ls-remote",
            "--exit-code",
            push_url,
            f"refs/heads/{lease.branch}",
        )
        remote_rows = [line.split("\t", 1) for line in remote_head.stdout.splitlines()]
        local_clean = self.worktrees.run_git(
            "-C", lease.worktree_path, "status", "--porcelain"
        )
        if (
            remote_head.returncode != 0
            or len(remote_rows) != 1
            or remote_rows[0] != [commit_sha, f"refs/heads/{lease.branch}"]
            or git_text("-C", lease.worktree_path, "rev-parse", "HEAD") != commit_sha
            or local_clean.returncode != 0
            or local_clean.stdout.strip()
        ):
            return record(
                GitDeliveryStatus.BLOCKED,
                "push_failed",
                commit_sha,
                "Push could not be verified; the local commit remains preserved.",
            )
        return record(
            GitDeliveryStatus.PUSHED,
            GitDeliveryStatus.PUSHED.value,
            commit_sha,
            f"Remote branch {lease.branch} was verified at the recorded commit.",
        )

    def workspace_for_run(self, run_id: str) -> WorkspaceLease | None:
        run_id = _required(run_id, "run id")
        return self.store.get_lease_for_agent(f"dashboard-run:{run_id}")

    def cleanup_dashboard_run_workspaces(self) -> tuple[WorkspaceLease, ...]:
        cleaned: list[WorkspaceLease] = []
        for lease in self.store.dashboard_run_leases():
            if lease.status is LeaseStatus.ACTIVE:
                lease = self.store.stop_lease(
                    lease.lease_id, "Dashboard worker did not survive service restart"
                )
            worktree = Path(lease.worktree_path)
            if worktree.is_dir():
                try:
                    if not self.worktrees.clean(worktree):
                        cleaned.append(lease)
                        continue
                except WorkflowError:
                    cleaned.append(lease)
                    continue
            cleaned.append(self.worktrees.release(lease.lease_id))
        return tuple(cleaned)

    def release_workspace(self, lease_id: str) -> WorkspaceLease:
        return self.worktrees.release(lease_id)

    def discard_workspace(self, lease_id: str, reason: str) -> WorkspaceLease:
        """Stop and remove a repair workspace without releasing dirty files."""

        lease = self.store.get_lease(lease_id)
        if lease is None:
            raise WorkflowError(f"Unknown workspace lease: {lease_id}")
        if lease.status is LeaseStatus.ACTIVE:
            self.stop(lease_id, reason)
        elif lease.status is LeaseStatus.RETAINED:
            self.store.stop_lease(lease_id, reason)
        return self.worktrees.release(lease_id)

    def before_model_call(self, lease_id: str) -> GateResult:
        lease = self._active_lease(lease_id)
        checks = self.checks.run(Path(lease.worktree_path))
        latest = self.store.latest_handoff(lease_id)
        handoff_allowed = latest is None or latest.status is HandoffStatus.ACCEPTED
        failed_checks = not all(check.passed for check in checks)
        required_action = None
        if not handoff_allowed and latest is not None:
            required_action = (
                latest.required_action
                if latest.required_action
                else f"Resolve handoff status: {latest.status.value}"
            )
        elif failed_checks:
            required_action = "Fix deterministic checks before another model call"
        result = GateResult(
            "model_call",
            not failed_checks and handoff_allowed,
            checks,
            required_action,
        )
        self.store.record_gate(lease_id, result)
        return result

    def verify_repair(
        self,
        lease_id: str,
        commit_sha: str,
        source_state: str = "conflict repair",
    ) -> GateResult:
        """Run and persist the full committed-tree evidence for a repair."""

        lease = self._active_lease(lease_id)
        rules = self.constitution.rules_for(WorkflowRole.WRITER)
        checks = self._handoff_checks(
            lease,
            _optional(source_state, 1_000),
            rules,
            _optional(commit_sha, 200),
        )
        result = GateResult(
            "conflict_repair",
            all(check.passed for check in checks),
            tuple(checks),
            None
            if all(check.passed for check in checks)
            else self._failed_action(checks),
        )
        self.store.record_gate(lease_id, result)
        return result

    def handoff(
        self,
        lease_id: str,
        source_role: WorkflowRole | str,
        target_role: WorkflowRole | str,
        commit_sha: str,
        source_state: str,
        approval_required: bool = False,
    ) -> HandoffRecord:
        lease = self._active_lease(lease_id)
        source = _enum_role(source_role)
        target = _enum_role(target_role)
        if target not in ALLOWED_ROLE_TRANSITIONS[source]:
            raise WorkflowError(
                "Workflow handoff from "
                f"{source.value} to {target.value} is not permitted"
            )
        rules = self._rules_for(source, target)
        normalized_commit = _optional(commit_sha, 200)
        normalized_state = _optional(source_state, 1_000)
        checks = self._handoff_checks(lease, normalized_state, rules, normalized_commit)
        passed = all(check.passed for check in checks)
        if not passed:
            status = HandoffStatus.BLOCKED
            required_action = self._failed_action(checks)
        elif approval_required or target is WorkflowRole.OPERATOR:
            status = HandoffStatus.AWAITING_APPROVAL
            required_action = "Operator approval is required before continuation"
        else:
            status = HandoffStatus.ACCEPTED
            required_action = None
        return self.store.record_handoff(
            lease_id,
            source,
            target,
            normalized_commit,
            normalized_state,
            status,
            checks,
            rules,
            required_action,
        )

    def approve_handoff(
        self, handoff_id: str, actor: str, note: str = ""
    ) -> HandoffRecord:
        handoff = self._handoff(handoff_id)
        if handoff.status is not HandoffStatus.AWAITING_APPROVAL:
            raise WorkflowError("Only a handoff awaiting approval can be approved")
        actor = _required(actor, "approval actor")
        lease = self._active_lease(handoff.lease_id)
        checks = self._handoff_checks(
            lease,
            handoff.source_state,
            handoff.constitution_rules,
            handoff.commit_sha,
        )
        if not all(check.passed for check in checks):
            return self.store.update_handoff(
                handoff_id,
                HandoffStatus.BLOCKED,
                self._failed_action(checks),
                checks,
                expected_status=HandoffStatus.AWAITING_APPROVAL,
            )
        return self.store.update_handoff(
            handoff_id,
            HandoffStatus.ACCEPTED,
            None,
            checks,
            actor,
            _optional(note, 1_000) or None,
            expected_status=HandoffStatus.AWAITING_APPROVAL,
        )

    def request_clarification(self, handoff_id: str, question: str) -> HandoffRecord:
        handoff = self._handoff(handoff_id)
        if handoff.status in {HandoffStatus.ACCEPTED, HandoffStatus.STOPPED}:
            raise WorkflowError("This handoff cannot be paused for clarification")
        return self.store.update_handoff(
            handoff_id,
            HandoffStatus.AWAITING_CLARIFICATION,
            _required(question, "clarification question", 1_000),
            expected_status=handoff.status,
        )

    def answer_clarification(self, handoff_id: str, answer: str) -> HandoffRecord:
        handoff = self._handoff(handoff_id)
        if handoff.status is not HandoffStatus.AWAITING_CLARIFICATION:
            raise WorkflowError("This handoff is not awaiting clarification")
        answer = _required(answer, "clarification answer", 1_000)
        return self.store.update_handoff(
            handoff_id,
            HandoffStatus.BLOCKED,
            f"Re-submit the handoff after clarification: {answer}",
            expected_status=HandoffStatus.AWAITING_CLARIFICATION,
        )

    def stop(self, lease_id: str, reason: str) -> WorkspaceLease:
        lease = self._active_lease(lease_id)
        _required(reason, "stop reason")
        return self.store.stop_lease(lease.lease_id, reason)

    def get_handoff(self, handoff_id: str) -> HandoffRecord:
        return self._handoff(handoff_id)

    def _active_lease(self, lease_id: str) -> WorkspaceLease:
        lease = self.store.get_lease(lease_id)
        if lease is None:
            raise WorkflowError(f"Unknown workspace lease: {lease_id}")
        if lease.status is not LeaseStatus.ACTIVE:
            raise WorkflowError(f"Workspace lease is {lease.status.value}")
        return lease

    def _handoff(self, handoff_id: str) -> HandoffRecord:
        handoff = self.store.get_handoff(handoff_id)
        if handoff is None:
            raise WorkflowError(f"Unknown handoff: {handoff_id}")
        return handoff

    def _rules_for(self, source: WorkflowRole, target: WorkflowRole) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                (
                    *self.constitution.rules_for(source),
                    *self.constitution.rules_for(target),
                )
            )
        )

    def _handoff_checks(
        self,
        lease: WorkspaceLease,
        source_state: str,
        rules: Sequence[str],
        commit_sha: str,
    ) -> list[CheckResult]:
        checks = list(self.checks.run(Path(lease.worktree_path)))
        if not source_state:
            checks.append(
                CheckResult("source_state", False, "Source state is required")
            )
        else:
            checks.append(CheckResult("source_state", True, source_state))
        checks.append(
            CheckResult(
                "constitution",
                bool(rules),
                f"Loaded {len(rules)} applicable constitution rules",
            )
        )
        if not commit_sha:
            checks.append(CheckResult("commit", False, "A committed SHA is required"))
        else:
            checks.append(self._commit_check(Path(lease.worktree_path), commit_sha))
        checks.append(self._clean_check(Path(lease.worktree_path)))
        return checks

    def _commit_check(self, workspace: Path, expected: str) -> CheckResult:
        try:
            actual = self.worktrees.head(workspace)
        except WorkflowError as exc:
            return CheckResult("commit", False, str(exc))
        return CheckResult(
            "commit",
            actual == expected,
            f"HEAD {actual}; expected {expected}",
        )

    def _clean_check(self, workspace: Path) -> CheckResult:
        try:
            clean = self.worktrees.clean(workspace)
        except WorkflowError as exc:
            return CheckResult("working_tree", False, str(exc))
        return CheckResult(
            "working_tree",
            clean,
            "Working tree is clean"
            if clean
            else "Working tree has uncommitted changes",
        )

    @staticmethod
    def _failed_action(checks: Sequence[CheckResult]) -> str:
        failed = ", ".join(check.name for check in checks if not check.passed)
        return f"Resolve failed or missing evidence: {failed}"
