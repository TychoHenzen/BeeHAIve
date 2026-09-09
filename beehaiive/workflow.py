"""Durable isolation, handoff, and deterministic quality gates."""

from __future__ import annotations

import json
import sqlite3
import subprocess
from collections.abc import Generator, Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from threading import RLock
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
    RELEASED = "released"
    STOPPED = "stopped"


class HandoffStatus(StrEnum):
    """State exposed to the next role or to an operator."""

    BLOCKED = "blocked"
    AWAITING_APPROVAL = "awaiting_approval"
    AWAITING_CLARIFICATION = "awaiting_clarification"
    ACCEPTED = "accepted"
    STOPPED = "stopped"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _required(value: str, label: str, limit: int = 400) -> str:
    normalized = " ".join(value.split())
    if not normalized:
        raise WorkflowError(f"{label} is required")
    if len(normalized) > limit:
        raise WorkflowError(f"{label} must be at most {limit} characters")
    return normalized


def _optional(value: str, limit: int = 400) -> str:
    normalized = " ".join(value.split())
    if len(normalized) > limit:
        raise WorkflowError(f"Value must be at most {limit} characters")
    return normalized


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

    def __init__(self, database: str | Path = ":memory:") -> None:
        if database != ":memory:":
            Path(database).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            str(database), check_same_thread=False, isolation_level=None
        )
        self._connection.row_factory = sqlite3.Row
        self._lock = RLock()
        self._initialize()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    @contextmanager
    def _transaction(self) -> Generator[sqlite3.Connection]:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
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
                    status TEXT NOT NULL,
                    stop_reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS active_workflow_branch
                    ON workflow_leases(branch) WHERE status = 'active';
                CREATE UNIQUE INDEX IF NOT EXISTS active_workflow_worktree
                    ON workflow_leases(worktree_path) WHERE status = 'active';
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
                """
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
            stop_reason=row["stop_reason"],
        )

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

    def get_lease(self, lease_id: str) -> WorkspaceLease | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM workflow_leases WHERE lease_id = ?", (lease_id,)
            ).fetchone()
        return None if row is None else self._lease_from_row(row)

    def acquire_lease(
        self, agent_id: str, branch: str, worktree_path: str
    ) -> WorkspaceLease:
        agent_id = _required(agent_id, "agent id")
        branch = _required(branch, "branch")
        worktree_path = _required(worktree_path, "worktree path", 1_000)
        timestamp = _now()
        lease_id = str(uuid4())
        try:
            with self._transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO workflow_leases(
                        lease_id, agent_id, branch, worktree_path, status,
                        stop_reason, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?)
                    """,
                    (
                        lease_id,
                        agent_id,
                        branch,
                        worktree_path,
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

    def release_lease(self, lease_id: str) -> WorkspaceLease:
        return self._set_lease_status(lease_id, LeaseStatus.RELEASED, None)

    def stop_lease(self, lease_id: str, reason: str) -> WorkspaceLease:
        return self._set_lease_status(
            lease_id, LeaseStatus.STOPPED, _required(reason, "stop reason")
        )

    def _set_lease_status(
        self, lease_id: str, status: LeaseStatus, reason: str | None
    ) -> WorkspaceLease:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM workflow_leases WHERE lease_id = ?", (lease_id,)
            ).fetchone()
            if row is None:
                raise WorkflowError(f"Unknown workspace lease: {lease_id}")
            if LeaseStatus(str(row["status"])) is not LeaseStatus.ACTIVE:
                return self._lease_from_row(row)
            connection.execute(
                """
                UPDATE workflow_leases
                SET status = ?, stop_reason = ?, updated_at = ?
                WHERE lease_id = ?
                """,
                (status.value, reason, _now(), lease_id),
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
                "SELECT lease_id FROM workflow_leases WHERE lease_id = ?",
                (lease_id,),
            ).fetchone()
            if lease is None:
                raise WorkflowError(f"Unknown workspace lease: {lease_id}")
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
    ) -> HandoffRecord:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM workflow_handoffs WHERE handoff_id = ?",
                (handoff_id,),
            ).fetchone()
            if row is None:
                raise WorkflowError(f"Unknown handoff: {handoff_id}")
            connection.execute(
                """
                UPDATE workflow_handoffs
                SET status = ?, required_action = ?, checks_json = ?,
                    approval_actor = ?, approval_note = ?, updated_at = ?
                WHERE handoff_id = ?
                """,
                (
                    status.value,
                    required_action,
                    _checks_to_json(checks)
                    if checks is not None
                    else str(row["checks_json"]),
                    approval_actor
                    if approval_actor is not None
                    else row["approval_actor"],
                    approval_note
                    if approval_note is not None
                    else row["approval_note"],
                    _now(),
                    handoff_id,
                ),
            )
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


class GitWorktreeManager:
    """Create and remove real git worktrees behind durable lease claims."""

    def __init__(self, repository: str | Path, store: WorkflowStore) -> None:
        self.repository = Path(repository).resolve()
        self.store = store
        if not self.repository.exists():
            raise WorkflowError(f"Repository does not exist: {self.repository}")

    def acquire(
        self, agent_id: str, branch: str, worktree: str | Path, base_ref: str = "HEAD"
    ) -> WorkspaceLease:
        branch = _required(branch, "branch")
        base_ref = _required(base_ref, "base ref")
        path = Path(worktree).resolve()
        lease = self.store.acquire_lease(agent_id, branch, str(path))
        try:
            self._git("worktree", "add", "-b", branch, str(path), base_ref)
        except Exception:
            self.store.release_lease(lease.lease_id)
            raise
        return lease

    def release(self, lease_id: str) -> WorkspaceLease:
        lease = self.store.get_lease(lease_id)
        if lease is None:
            raise WorkflowError(f"Unknown workspace lease: {lease_id}")
        if lease.status is LeaseStatus.ACTIVE:
            self._git("worktree", "remove", lease.worktree_path)
        return self.store.release_lease(lease_id)

    def head(self, worktree: str | Path) -> str:
        return self._git("-C", str(Path(worktree)), "rev-parse", "HEAD")

    def clean(self, worktree: str | Path) -> bool:
        return not self._git("-C", str(Path(worktree)), "status", "--porcelain")

    def _git(self, *arguments: str) -> str:
        result = subprocess.run(
            ("git", *arguments),
            cwd=self.repository,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            message = (result.stderr or result.stdout).strip()
            raise WorkflowError(message or f"git command failed: {' '.join(arguments)}")
        return result.stdout.strip()


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

    def release_workspace(self, lease_id: str) -> WorkspaceLease:
        return self.worktrees.release(lease_id)

    def before_model_call(self, lease_id: str) -> GateResult:
        lease = self._active_lease(lease_id)
        checks = self.checks.run(Path(lease.worktree_path))
        result = GateResult(
            "model_call",
            all(check.passed for check in checks),
            checks,
            None
            if all(check.passed for check in checks)
            else "Fix deterministic checks before another model call",
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
        rules = self._rules_for(source, target)
        normalized_commit = _optional(commit_sha, 200)
        normalized_state = _optional(source_state, 1_000)
        checks = list(self.checks.run(Path(lease.worktree_path)))
        if not normalized_state:
            checks.append(
                CheckResult("source_state", False, "Source state is required")
            )
        else:
            checks.append(CheckResult("source_state", True, normalized_state))
        checks.append(
            CheckResult(
                "constitution",
                bool(rules),
                f"Loaded {len(rules)} applicable constitution rules",
            )
        )
        if not normalized_commit:
            checks.append(CheckResult("commit", False, "A committed SHA is required"))
        else:
            checks.append(
                self._commit_check(Path(lease.worktree_path), normalized_commit)
            )
        checks.append(self._clean_check(Path(lease.worktree_path)))
        passed = all(check.passed for check in checks)
        if not passed:
            status = HandoffStatus.BLOCKED
            required_action = self._failed_action(checks)
        elif approval_required:
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
        checks = list(self.checks.run(Path(lease.worktree_path)))
        checks.append(CheckResult("source_state", True, handoff.source_state))
        checks.append(
            CheckResult(
                "constitution",
                bool(handoff.constitution_rules),
                "Loaded "
                f"{len(handoff.constitution_rules)} applicable constitution rules",
            )
        )
        checks.append(self._commit_check(Path(lease.worktree_path), handoff.commit_sha))
        checks.append(self._clean_check(Path(lease.worktree_path)))
        if not all(check.passed for check in checks):
            return self.store.update_handoff(
                handoff_id,
                HandoffStatus.BLOCKED,
                self._failed_action(checks),
                checks,
            )
        return self.store.update_handoff(
            handoff_id,
            HandoffStatus.ACCEPTED,
            None,
            checks,
            actor,
            _optional(note, 1_000) or None,
        )

    def request_clarification(self, handoff_id: str, question: str) -> HandoffRecord:
        handoff = self._handoff(handoff_id)
        if handoff.status in {HandoffStatus.ACCEPTED, HandoffStatus.STOPPED}:
            raise WorkflowError("This handoff cannot be paused for clarification")
        return self.store.update_handoff(
            handoff_id,
            HandoffStatus.AWAITING_CLARIFICATION,
            _required(question, "clarification question", 1_000),
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
        )

    def stop(self, lease_id: str, reason: str) -> WorkspaceLease:
        lease = self._active_lease(lease_id)
        handoff = self.store.latest_handoff(lease_id)
        if handoff is not None and handoff.status not in {
            HandoffStatus.ACCEPTED,
            HandoffStatus.STOPPED,
        }:
            self.store.update_handoff(
                handoff.handoff_id,
                HandoffStatus.STOPPED,
                _required(reason, "stop reason"),
            )
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
