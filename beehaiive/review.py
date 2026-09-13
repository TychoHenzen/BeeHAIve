"""Durable pull-request review cycles and merge gating."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Generator, Iterable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from threading import RLock
from typing import Protocol, cast
from uuid import uuid4

MAX_REVIEW_EVIDENCE_BYTES = 1_000_000
MAX_REVIEW_EVIDENCE_REFS = 100


class ReviewError(RuntimeError):
    """Raised when a review cycle cannot accept a state transition."""


class ReviewAdapterError(ReviewError):
    """Raised when a provider or reader adapter fails outside review state."""


class ReviewConcern(StrEnum):
    """Required specialized readers for one review cycle."""

    SECURITY = "security"
    TEST_COVERAGE = "test_coverage"
    CLEAN_CODE = "clean_code"
    PERFORMANCE = "performance"


REQUIRED_CONCERNS = (
    ReviewConcern.SECURITY,
    ReviewConcern.TEST_COVERAGE,
    ReviewConcern.CLEAN_CODE,
    ReviewConcern.PERFORMANCE,
)


@dataclass(frozen=True, slots=True)
class PullRequestTarget:
    """Current provider-backed pull-request state used by a review run."""

    pull_request_id: str
    head_sha: str
    ready: bool = True
    evidence_json: str | None = None


@dataclass(frozen=True, slots=True)
class ReaderExecution:
    """Result returned by one specialized reader double or adapter."""

    status: ReaderStatus | str
    findings: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()


class PullRequestReviewProvider(Protocol):
    """Provider that returns the current head before review or merge."""

    def get_pull_request(self, pull_request_id: str) -> PullRequestTarget: ...


class ReviewReader(Protocol):
    """Specialized reader invoked for one pull-request concern."""

    def review(self, target: PullRequestTarget) -> ReaderExecution: ...


class ReviewAction(StrEnum):
    """Closed set of actions that the review authorizer may permit."""

    START = "start"
    READ = "read"
    READER = "reader"
    WRITER = "writer"
    APPROVE = "approve"
    HANDOFF = "handoff"


class ReviewAuthorizer(Protocol):
    """Authorization policy for review actions."""

    def authorize(
        self, pull_request_id: str, actor: str, action: ReviewAction | str
    ) -> bool: ...


class AllowListReviewAuthorizer:
    """Default policy that reserves approval for named human operators."""

    def __init__(
        self,
        human_actors: Iterable[str] = ("operator",),
        reader_actors: Iterable[str] = ("reader", "operator"),
        writer_actors: Iterable[str] = ("writer", "operator"),
    ) -> None:
        self._human_actors = frozenset(human_actors)
        self._reader_actors = frozenset(reader_actors)
        self._writer_actors = frozenset(writer_actors)

    def authorize(
        self, pull_request_id: str, actor: str, action: ReviewAction | str
    ) -> bool:
        del pull_request_id
        normalized_actor = actor.strip()
        if not normalized_actor:
            return False
        try:
            resolved_action = ReviewAction(action)
        except ValueError:
            return False
        allowed_actors = {
            ReviewAction.START: self._writer_actors,
            ReviewAction.READ: self._human_actors
            | self._reader_actors
            | self._writer_actors,
            ReviewAction.READER: self._reader_actors,
            ReviewAction.WRITER: self._writer_actors,
            ReviewAction.APPROVE: self._human_actors,
            ReviewAction.HANDOFF: self._human_actors,
        }
        return normalized_actor in allowed_actors[resolved_action]


class ReaderStatus(StrEnum):
    """State returned by one specialized reader."""

    PENDING = "pending"
    PASS = "pass"
    FAIL = "fail"


class ReviewCycleStatus(StrEnum):
    """Lifecycle state of one pull-request review cycle."""

    ACTIVE = "active"
    PASSED = "passed"
    FAILED = "failed"
    HUMAN_APPROVED = "human_approved"
    SUPERSEDED = "superseded"


class FindingStatus(StrEnum):
    """Resolution state of a reader finding."""

    OPEN = "open"
    RESOLVED = "resolved"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _claim_is_active(expires_at: str | None) -> bool:
    if not expires_at:
        return False
    try:
        return datetime.fromisoformat(expires_at) > datetime.now(UTC)
    except ValueError:
        return False


def _required(value: str, label: str, limit: int = 200) -> str:
    normalized = " ".join(value.split())
    if not normalized:
        raise ReviewError(f"{label} is required")
    if len(normalized) > limit:
        raise ReviewError(f"{label} must be at most {limit} characters")
    return normalized


def _head_sha(value: str, label: str = "head SHA") -> str:
    normalized = _required(value, label)
    if normalized != value or any(
        not (character.isalnum() or character in "._/-") for character in normalized
    ):
        raise ReviewError(f"{label} has an invalid format")
    return normalized


def _enum[T: StrEnum](value: T | str, enum_type: type[T], label: str) -> T:
    try:
        return enum_type(value)
    except ValueError as exc:
        raise ReviewError(f"Unknown {label}: {value}") from exc


def _json_list(value: str) -> tuple[str, ...]:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ReviewError("Stored reader findings are invalid") from exc
    if not isinstance(decoded, list):
        raise ReviewError("Stored reader findings are invalid")
    items = cast(list[object], decoded)
    if not all(isinstance(item, str) for item in items):
        raise ReviewError("Stored reader findings are invalid")
    return tuple(cast(list[str], items))


def _json_object(value: str) -> dict[str, object]:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ReviewError("Stored GitHub review evidence is invalid") from exc
    if not isinstance(decoded, dict):
        raise ReviewError("Stored GitHub review evidence is invalid")
    return cast(dict[str, object], decoded)


def github_pull_request_evidence_ref(evidence_json: str | None) -> str | None:
    if evidence_json is None:
        return None
    evidence = _json_object(evidence_json)
    pull_request = evidence.get("pull_request")
    if not isinstance(pull_request, dict):
        return None
    identifier = cast(dict[str, object], pull_request).get("id")
    return identifier if isinstance(identifier, str) and identifier else None


def _evidence_refs(values: Iterable[str]) -> tuple[str, ...]:
    refs = tuple(
        dict.fromkeys(_required(value, "evidence reference", 500) for value in values)
    )
    if len(refs) > MAX_REVIEW_EVIDENCE_REFS:
        raise ReviewError(
            f"A finding can reference at most {MAX_REVIEW_EVIDENCE_REFS} evidence items"
        )
    return refs


def _validate_evidence_refs(
    evidence_json: str | None, refs: tuple[str, ...], *, required: bool
) -> None:
    if evidence_json is None:
        if refs:
            raise ReviewError("GitHub evidence references require a provider snapshot")
        return
    if required and not refs:
        raise ReviewError("A GitHub-backed result must reference its evidence")
    source_ids: set[str] = set()

    def collect_ids(value: object) -> None:
        if isinstance(value, Mapping):
            mapping = cast(Mapping[str, object], value)
            identifier = mapping.get("id")
            if isinstance(identifier, str) and identifier:
                source_ids.add(identifier)
            for nested in mapping.values():
                collect_ids(nested)
        elif isinstance(value, list):
            for nested in cast(list[object], value):
                collect_ids(nested)

    collect_ids(_json_object(evidence_json))
    if any(ref not in source_ids for ref in refs):
        raise ReviewError("GitHub evidence reference is not in the provider snapshot")


@dataclass(frozen=True, slots=True)
class ReviewCycle:
    cycle_id: str
    pull_request_id: str
    head_sha: str
    cycle_number: int
    status: ReviewCycleStatus
    human_approval: bool
    required_action: str | None
    created_at: str
    updated_at: str
    approval_actor: str | None = None
    approval_reason: str | None = None
    approval_at: str | None = None
    github_evidence_json: str | None = None

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "cycle_id": self.cycle_id,
            "pull_request_id": self.pull_request_id,
            "head_sha": self.head_sha,
            "cycle_number": self.cycle_number,
            "status": self.status.value,
            "human_approval": self.human_approval,
            "required_action": self.required_action,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "approval_actor": self.approval_actor,
            "approval_reason": self.approval_reason,
            "approval_at": self.approval_at,
        }
        if self.github_evidence_json is not None:
            result["github_evidence"] = _json_object(self.github_evidence_json)
        return result


@dataclass(frozen=True, slots=True)
class ReaderResult:
    cycle_id: str
    concern: ReviewConcern
    status: ReaderStatus
    finding_ids: tuple[str, ...]
    reader: str
    updated_at: str
    evidence_refs: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "cycle_id": self.cycle_id,
            "concern": self.concern.value,
            "status": self.status.value,
            "finding_ids": list(self.finding_ids),
            "reader": self.reader,
            "updated_at": self.updated_at,
        }
        if self.evidence_refs:
            result["evidence_refs"] = list(self.evidence_refs)
        return result


@dataclass(frozen=True, slots=True)
class ReviewFinding:
    finding_id: str
    pull_request_id: str
    cycle_id: str
    concern: ReviewConcern
    summary: str
    status: FindingStatus
    resolution: str | None
    created_at: str
    updated_at: str
    evidence_refs: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "finding_id": self.finding_id,
            "pull_request_id": self.pull_request_id,
            "cycle_id": self.cycle_id,
            "concern": self.concern.value,
            "summary": self.summary,
            "status": self.status.value,
            "resolution": self.resolution,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        if self.evidence_refs:
            result["evidence_refs"] = list(self.evidence_refs)
        return result


@dataclass(frozen=True, slots=True)
class ReviewSnapshot:
    cycle: ReviewCycle
    readers: tuple[ReaderResult, ...]
    findings: tuple[ReviewFinding, ...]
    merge_allowed: bool

    def as_dict(self) -> dict[str, object]:
        open_findings = [
            finding.as_dict()
            for finding in self.findings
            if finding.status is FindingStatus.OPEN
        ]
        return {
            "cycle": self.cycle.as_dict(),
            "readers": [reader.as_dict() for reader in self.readers],
            "findings": [finding.as_dict() for finding in self.findings],
            "writer_feedback": open_findings,
            "merge_allowed": self.merge_allowed,
        }


@dataclass(frozen=True, slots=True)
class MergeHandoff:
    pull_request_id: str
    cycle_id: str
    head_sha: str
    approved_by_human: bool
    approval_actor: str | None = None
    approval_reason: str | None = None
    approval_at: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "pull_request_id": self.pull_request_id,
            "cycle_id": self.cycle_id,
            "head_sha": self.head_sha,
            "approved_by_human": self.approved_by_human,
            "approval_actor": self.approval_actor,
            "approval_reason": self.approval_reason,
            "approval_at": self.approval_at,
            "status": "merge_handoff",
        }


class ReviewStore:
    """Thread-safe SQLite persistence for review cycles and findings."""

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
    def transaction(self) -> Generator[sqlite3.Connection]:
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
                CREATE TABLE IF NOT EXISTS review_cycles (
                    cycle_id TEXT PRIMARY KEY,
                    pull_request_id TEXT NOT NULL,
                    head_sha TEXT NOT NULL,
                    cycle_number INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    human_approval INTEGER NOT NULL DEFAULT 0,
                    required_action TEXT,
                    approval_actor TEXT,
                    approval_reason TEXT,
                    approval_at TEXT,
                    github_evidence_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(pull_request_id, cycle_number)
                );
                CREATE TABLE IF NOT EXISTS review_readers (
                    cycle_id TEXT NOT NULL,
                    concern TEXT NOT NULL,
                    status TEXT NOT NULL,
                    finding_ids_json TEXT NOT NULL,
                    reader TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    claim_token TEXT,
                    claim_expires_at TEXT,
                    evidence_refs_json TEXT NOT NULL DEFAULT '[]',
                    PRIMARY KEY(cycle_id, concern),
                    FOREIGN KEY(cycle_id) REFERENCES review_cycles(cycle_id)
                );
                CREATE TABLE IF NOT EXISTS review_findings (
                    finding_id TEXT PRIMARY KEY,
                    pull_request_id TEXT NOT NULL,
                    cycle_id TEXT NOT NULL,
                    concern TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    status TEXT NOT NULL,
                    resolution TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    evidence_refs_json TEXT NOT NULL DEFAULT '[]',
                    FOREIGN KEY(cycle_id) REFERENCES review_cycles(cycle_id)
                );
                CREATE INDEX IF NOT EXISTS review_cycles_by_pull_request
                    ON review_cycles(pull_request_id, cycle_number DESC);
                CREATE INDEX IF NOT EXISTS review_findings_by_pull_request
                    ON review_findings(pull_request_id, created_at);
                """
            )
            cycle_columns = {
                str(row["name"])
                for row in self._connection.execute(
                    "PRAGMA table_info(review_cycles)"
                ).fetchall()
            }
            for column in (
                "approval_actor",
                "approval_reason",
                "approval_at",
                "github_evidence_json",
            ):
                if column not in cycle_columns:
                    self._connection.execute(
                        f"ALTER TABLE review_cycles ADD COLUMN {column} TEXT"
                    )
            reader_columns = {
                str(row["name"])
                for row in self._connection.execute(
                    "PRAGMA table_info(review_readers)"
                ).fetchall()
            }
            for column in ("claim_token", "claim_expires_at"):
                if column not in reader_columns:
                    self._connection.execute(
                        f"ALTER TABLE review_readers ADD COLUMN {column} TEXT"
                    )
            if "evidence_refs_json" not in reader_columns:
                self._connection.execute(
                    "ALTER TABLE review_readers ADD COLUMN "
                    "evidence_refs_json TEXT NOT NULL DEFAULT '[]'"
                )
            finding_columns = {
                str(row["name"])
                for row in self._connection.execute(
                    "PRAGMA table_info(review_findings)"
                ).fetchall()
            }
            if "evidence_refs_json" not in finding_columns:
                self._connection.execute(
                    "ALTER TABLE review_findings ADD COLUMN "
                    "evidence_refs_json TEXT NOT NULL DEFAULT '[]'"
                )

    def _cycle_from_row(self, row: sqlite3.Row) -> ReviewCycle:
        return ReviewCycle(
            cycle_id=str(row["cycle_id"]),
            pull_request_id=str(row["pull_request_id"]),
            head_sha=str(row["head_sha"]),
            cycle_number=int(row["cycle_number"]),
            status=ReviewCycleStatus(str(row["status"])),
            human_approval=bool(row["human_approval"]),
            required_action=row["required_action"],
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            approval_actor=row["approval_actor"],
            approval_reason=row["approval_reason"],
            approval_at=row["approval_at"],
            github_evidence_json=row["github_evidence_json"],
        )

    def _reader_from_row(self, row: sqlite3.Row) -> ReaderResult:
        return ReaderResult(
            cycle_id=str(row["cycle_id"]),
            concern=ReviewConcern(str(row["concern"])),
            status=ReaderStatus(str(row["status"])),
            finding_ids=_json_list(str(row["finding_ids_json"])),
            reader=str(row["reader"]),
            updated_at=str(row["updated_at"]),
            evidence_refs=_json_list(str(row["evidence_refs_json"])),
        )

    def _finding_from_row(self, row: sqlite3.Row) -> ReviewFinding:
        return ReviewFinding(
            finding_id=str(row["finding_id"]),
            pull_request_id=str(row["pull_request_id"]),
            cycle_id=str(row["cycle_id"]),
            concern=ReviewConcern(str(row["concern"])),
            summary=str(row["summary"]),
            status=FindingStatus(str(row["status"])),
            resolution=row["resolution"],
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            evidence_refs=_json_list(str(row["evidence_refs_json"])),
        )

    def cycle_row(
        self, connection: sqlite3.Connection, cycle_id: str
    ) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM review_cycles WHERE cycle_id = ?", (cycle_id,)
        ).fetchone()

    def current_cycle_row(
        self, connection: sqlite3.Connection, pull_request_id: str
    ) -> sqlite3.Row | None:
        return connection.execute(
            """
            SELECT * FROM review_cycles
            WHERE pull_request_id = ?
            ORDER BY cycle_number DESC
            LIMIT 1
            """,
            (pull_request_id,),
        ).fetchone()

    def current_cycle_id(self, pull_request_id: str) -> str | None:
        with self._lock:
            row = self.current_cycle_row(self._connection, pull_request_id)
        return None if row is None else str(row["cycle_id"])

    def claim_reader(self, cycle_id: str, concern: ReviewConcern) -> str | None:
        with self.transaction() as connection:
            cycle = self.cycle_row(connection, cycle_id)
            if cycle is None:
                raise ReviewError(f"Unknown review cycle: {cycle_id}")
            current = self.current_cycle_row(connection, str(cycle["pull_request_id"]))
            if current is None or str(current["cycle_id"]) != cycle_id:
                raise ReviewError("Reader result belongs to a stale review cycle")
            if ReviewCycleStatus(str(cycle["status"])) in {
                ReviewCycleStatus.SUPERSEDED,
                ReviewCycleStatus.HUMAN_APPROVED,
            }:
                raise ReviewError("Review cycle is no longer accepting reader results")
            reader = connection.execute(
                """
                SELECT status, claim_token, claim_expires_at
                FROM review_readers
                WHERE cycle_id = ? AND concern = ?
                """,
                (cycle_id, concern.value),
            ).fetchone()
            if reader is None:
                raise ReviewError(f"No reader is configured for {concern.value}")
            if ReaderStatus(str(reader["status"])) is not ReaderStatus.PENDING:
                return None
            if _claim_is_active(reader["claim_expires_at"]):
                return None
            claim_token = str(uuid4())
            claim_expires_at = (datetime.now(UTC) + timedelta(seconds=60)).isoformat()
            connection.execute(
                """
                UPDATE review_readers
                SET claim_token = ?, claim_expires_at = ?, updated_at = ?
                WHERE cycle_id = ? AND concern = ? AND status = ?
                """,
                (
                    claim_token,
                    claim_expires_at,
                    _now(),
                    cycle_id,
                    concern.value,
                    ReaderStatus.PENDING.value,
                ),
            )
            return claim_token

    def _readers(
        self, connection: sqlite3.Connection, cycle_id: str
    ) -> tuple[ReaderResult, ...]:
        rows = connection.execute(
            "SELECT * FROM review_readers WHERE cycle_id = ?",
            (cycle_id,),
        ).fetchall()
        readers = tuple(self._reader_from_row(row) for row in rows)
        return tuple(
            sorted(readers, key=lambda reader: REQUIRED_CONCERNS.index(reader.concern))
        )

    def _findings(
        self, connection: sqlite3.Connection, pull_request_id: str
    ) -> tuple[ReviewFinding, ...]:
        rows = connection.execute(
            """
            SELECT * FROM review_findings
            WHERE pull_request_id = ?
            ORDER BY created_at, finding_id
            """,
            (pull_request_id,),
        ).fetchall()
        return tuple(self._finding_from_row(row) for row in rows)

    def snapshot(self, cycle_id: str) -> ReviewSnapshot:
        with self._lock:
            return self.snapshot_in_connection(self._connection, cycle_id)

    def snapshot_in_connection(
        self, connection: sqlite3.Connection, cycle_id: str
    ) -> ReviewSnapshot:
        cycle_row = self.cycle_row(connection, cycle_id)
        if cycle_row is None:
            raise ReviewError(f"Unknown review cycle: {cycle_id}")
        cycle = self._cycle_from_row(cycle_row)
        readers = self._readers(connection, cycle_id)
        findings = self._findings(connection, cycle.pull_request_id)
        merge_allowed = cycle.status is ReviewCycleStatus.HUMAN_APPROVED or (
            cycle.status is ReviewCycleStatus.PASSED
            and not any(finding.status is FindingStatus.OPEN for finding in findings)
        )
        return ReviewSnapshot(cycle, readers, findings, merge_allowed)

    def current_snapshot(self, pull_request_id: str) -> ReviewSnapshot:
        with self._lock:
            row = self.current_cycle_row(self._connection, pull_request_id)
        if row is None:
            raise ReviewError(f"No review cycle exists for {pull_request_id}")
        return self.snapshot(str(row["cycle_id"]))

    def pull_request_id_for_cycle(self, cycle_id: str) -> str:
        with self._lock:
            row = self.cycle_row(self._connection, cycle_id)
        if row is None:
            raise ReviewError(f"Unknown review cycle: {cycle_id}")
        return str(row["pull_request_id"])

    def pull_request_id_for_finding(self, finding_id: str) -> str:
        with self._lock:
            row = self._connection.execute(
                "SELECT pull_request_id FROM review_findings WHERE finding_id = ?",
                (finding_id,),
            ).fetchone()
        if row is None:
            raise ReviewError(f"Unknown review finding: {finding_id}")
        return str(row["pull_request_id"])


class ReviewService:
    """Start review cycles, preserve findings, and gate merge handoffs."""

    def __init__(
        self,
        store: ReviewStore,
        provider: PullRequestReviewProvider | None = None,
        readers: Mapping[ReviewConcern, ReviewReader] | None = None,
        authorizer: ReviewAuthorizer | None = None,
    ) -> None:
        self.store = store
        self.provider = provider
        self.readers = dict(readers or {})
        self.authorizer = authorizer or AllowListReviewAuthorizer()

    def authorize(
        self, pull_request_id: str, actor: str, action: ReviewAction | str
    ) -> None:
        pull_request_id = _required(pull_request_id, "pull request id")
        actor = _required(actor, "review actor", 100)
        action = _enum(action, ReviewAction, "review action")
        if not self.authorizer.authorize(pull_request_id, actor, action):
            raise ReviewError("Review actor is not authorized for this action")

    def pull_request_id_for_cycle(self, cycle_id: str) -> str:
        return self.store.pull_request_id_for_cycle(cycle_id)

    def pull_request_id_for_finding(self, finding_id: str) -> str:
        return self.store.pull_request_id_for_finding(finding_id)

    def _validated_provider_target(self, pull_request_id: str) -> PullRequestTarget:
        if self.provider is None:
            raise ReviewError("A pull-request provider is required for ready reviews")
        try:
            target = self.provider.get_pull_request(pull_request_id)
        except ReviewError:
            raise
        except Exception as exc:
            raise ReviewAdapterError("Pull-request provider failed") from exc
        if target.pull_request_id != pull_request_id:
            raise ReviewError("Pull-request provider returned the wrong pull request")
        if not target.ready:
            raise ReviewError("Pull request is not ready for review")
        raw_evidence_json: object = getattr(target, "evidence_json", None)
        if raw_evidence_json is None:
            evidence_json = None
        elif isinstance(raw_evidence_json, str):
            evidence_json = raw_evidence_json
            if len(evidence_json.encode("utf-8")) > MAX_REVIEW_EVIDENCE_BYTES:
                raise ReviewError("GitHub review evidence exceeds the storage limit")
            _json_object(evidence_json)
            if github_pull_request_evidence_ref(evidence_json) is None:
                raise ReviewError(
                    "Provider evidence omitted the GitHub pull-request id"
                )
        else:
            raise ReviewError("Provider returned invalid GitHub review evidence")
        return PullRequestTarget(
            target.pull_request_id,
            _head_sha(target.head_sha, "Provider head SHA"),
            target.ready,
            evidence_json,
        )

    def run_ready_review(self, pull_request_id: str) -> ReviewSnapshot:
        pull_request_id = _required(pull_request_id, "pull request id")
        expected_cycle_id = self.store.current_cycle_id(pull_request_id)
        target = self._validated_provider_target(pull_request_id)
        missing = [
            concern.value
            for concern in REQUIRED_CONCERNS
            if concern not in self.readers
        ]
        if missing:
            raise ReviewError(f"No reader is configured for {', '.join(missing)}")
        cycle = self._start_cycle(
            pull_request_id,
            target.head_sha,
            expected_cycle_id=expected_cycle_id,
            github_evidence_json=target.evidence_json,
        )
        for concern in REQUIRED_CONCERNS:
            reader = self.readers[concern]
            current_reader = next(
                item for item in cycle.readers if item.concern is concern
            )
            if current_reader.status is not ReaderStatus.PENDING:
                continue
            claim_token = self.store.claim_reader(cycle.cycle.cycle_id, concern)
            if claim_token is None:
                continue
            try:
                execution = reader.review(target)
            except Exception:
                evidence_ref = github_pull_request_evidence_ref(target.evidence_json)
                execution = ReaderExecution(
                    ReaderStatus.FAIL,
                    (f"{concern.value} reader failed",),
                    (evidence_ref,) if evidence_ref is not None else (),
                )
            cycle = self.record_reader(
                cycle.cycle.cycle_id,
                concern,
                execution.status,
                execution.findings,
                reader=concern.value,
                claim_token=claim_token,
                evidence_refs=execution.evidence_refs,
            )
        return cycle

    def start_cycle(self, pull_request_id: str, head_sha: str) -> ReviewSnapshot:
        pull_request_id = _required(pull_request_id, "pull request id")
        head_sha = _head_sha(head_sha)
        expected_cycle_id = self.store.current_cycle_id(pull_request_id)
        evidence_json = None
        if self.provider is not None:
            target = self._validated_provider_target(pull_request_id)
            if target.head_sha != head_sha:
                raise ReviewError("Review head does not match the current pull request")
            evidence_json = target.evidence_json
        return self._start_cycle(
            pull_request_id,
            head_sha,
            expected_cycle_id=expected_cycle_id,
            github_evidence_json=evidence_json,
        )

    def _start_cycle(
        self,
        pull_request_id: str,
        head_sha: str,
        *,
        expected_cycle_id: str | None,
        github_evidence_json: str | None = None,
    ) -> ReviewSnapshot:
        with self.store.transaction() as connection:
            current = self.store.current_cycle_row(connection, pull_request_id)
            current_cycle_id = None if current is None else str(current["cycle_id"])
            if current_cycle_id != expected_cycle_id:
                if (
                    current is not None
                    and str(current["head_sha"]) == head_sha
                    and ReviewCycleStatus(str(current["status"]))
                    is not ReviewCycleStatus.FAILED
                ):
                    assert current_cycle_id is not None
                    return self.store.snapshot(current_cycle_id)
                raise ReviewError("Review cycle changed while starting a new cycle")
            if current is not None:
                current_status = ReviewCycleStatus(str(current["status"]))
                if (
                    str(current["head_sha"]) == head_sha
                    and current_status is not ReviewCycleStatus.FAILED
                ):
                    cycle_id = str(current["cycle_id"])
                    return self.store.snapshot(cycle_id)
                connection.execute(
                    """
                    UPDATE review_cycles
                    SET status = ?, required_action = ?, updated_at = ?
                    WHERE cycle_id = ?
                    """,
                    (
                        ReviewCycleStatus.SUPERSEDED.value,
                        "A newer review cycle must authorize merge",
                        _now(),
                        str(current["cycle_id"]),
                    ),
                )
                cycle_number = int(current["cycle_number"]) + 1
            else:
                cycle_number = 1
            cycle_id = str(uuid4())
            timestamp = _now()
            connection.execute(
                """
                INSERT INTO review_cycles(
                    cycle_id, pull_request_id, head_sha, cycle_number, status,
                    human_approval, required_action, github_evidence_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
                """,
                (
                    cycle_id,
                    pull_request_id,
                    head_sha,
                    cycle_number,
                    ReviewCycleStatus.ACTIVE.value,
                    "Awaiting four specialized review readers",
                    github_evidence_json,
                    timestamp,
                    timestamp,
                ),
            )
            for concern in REQUIRED_CONCERNS:
                connection.execute(
                    """
                    INSERT INTO review_readers(
                        cycle_id, concern, status, finding_ids_json, reader, updated_at
                    ) VALUES (?, ?, ?, '[]', 'automated', ?)
                    """,
                    (cycle_id, concern.value, ReaderStatus.PENDING.value, timestamp),
                )
        return self.store.snapshot(cycle_id)

    def snapshot(self, pull_request_id: str) -> ReviewSnapshot:
        return self.store.current_snapshot(
            _required(pull_request_id, "pull request id")
        )

    def record_reader(
        self,
        cycle_id: str,
        concern: ReviewConcern | str,
        status: ReaderStatus | str,
        findings: Iterable[str] = (),
        reader: str = "automated",
        claim_token: str | None = None,
        evidence_refs: Iterable[str] = (),
    ) -> ReviewSnapshot:
        concern = _enum(concern, ReviewConcern, "review concern")
        status = _enum(status, ReaderStatus, "reader status")
        reader = _required(reader, "reader", 100)
        summaries = tuple(
            _required(summary, "finding summary", 1_000) for summary in findings
        )
        refs = _evidence_refs(evidence_refs)
        if status is ReaderStatus.FAIL and not summaries:
            raise ReviewError("A failed reader must provide findings")
        if status is not ReaderStatus.FAIL and summaries:
            raise ReviewError("Only a failed reader can provide findings")
        with self.store.transaction() as connection:
            cycle = self.store.cycle_row(connection, cycle_id)
            if cycle is None:
                raise ReviewError(f"Unknown review cycle: {cycle_id}")
            _validate_evidence_refs(
                cycle["github_evidence_json"],
                refs,
                required=(
                    cycle["github_evidence_json"] is not None
                    and status in {ReaderStatus.PASS, ReaderStatus.FAIL}
                ),
            )
            current = self.store.current_cycle_row(
                connection, str(cycle["pull_request_id"])
            )
            if current is None or str(current["cycle_id"]) != cycle_id:
                raise ReviewError("Reader result belongs to a stale review cycle")
            if ReviewCycleStatus(str(cycle["status"])) in {
                ReviewCycleStatus.SUPERSEDED,
                ReviewCycleStatus.HUMAN_APPROVED,
            }:
                raise ReviewError("Review cycle is no longer accepting reader results")
            existing = connection.execute(
                "SELECT * FROM review_readers WHERE cycle_id = ? AND concern = ?",
                (cycle_id, concern.value),
            ).fetchone()
            if existing is None:
                raise ReviewError(f"No reader is configured for {concern.value}")
            if ReaderStatus(str(existing["status"])) is not ReaderStatus.PENDING:
                raise ReviewError(f"Reader result already recorded for {concern.value}")
            if claim_token is not None and (
                existing["claim_token"] != claim_token
                or not _claim_is_active(existing["claim_expires_at"])
            ):
                raise ReviewError("Reader claim is no longer valid")
            finding_ids: list[str] = []
            timestamp = _now()
            for summary in summaries:
                finding_id = str(uuid4())
                finding_ids.append(finding_id)
                connection.execute(
                    """
                    INSERT INTO review_findings(
                        finding_id, pull_request_id, cycle_id, concern, summary,
                        status, resolution, created_at, updated_at, evidence_refs_json
                    ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
                    """,
                    (
                        finding_id,
                        str(cycle["pull_request_id"]),
                        cycle_id,
                        concern.value,
                        summary,
                        FindingStatus.OPEN.value,
                        timestamp,
                        timestamp,
                        json.dumps(refs),
                    ),
                )
            connection.execute(
                """
                UPDATE review_readers
                SET status = ?, finding_ids_json = ?, reader = ?, updated_at = ?,
                    evidence_refs_json = ?,
                    claim_token = NULL, claim_expires_at = NULL
                WHERE cycle_id = ? AND concern = ?
                """,
                (
                    status.value,
                    json.dumps(finding_ids),
                    reader,
                    timestamp,
                    json.dumps(refs),
                    cycle_id,
                    concern.value,
                ),
            )
            self._recompute_cycle(connection, cycle_id)
        return self.store.snapshot(cycle_id)

    def add_finding(
        self,
        cycle_id: str,
        concern: ReviewConcern | str,
        summary: str,
        evidence_refs: Iterable[str] = (),
    ) -> ReviewSnapshot:
        concern = _enum(concern, ReviewConcern, "review concern")
        summary = _required(summary, "finding summary", 1_000)
        refs = _evidence_refs(evidence_refs)
        with self.store.transaction() as connection:
            cycle = self.store.cycle_row(connection, cycle_id)
            if cycle is None:
                raise ReviewError(f"Unknown review cycle: {cycle_id}")
            _validate_evidence_refs(
                cycle["github_evidence_json"],
                refs,
                required=cycle["github_evidence_json"] is not None,
            )
            current = self.store.current_cycle_row(
                connection, str(cycle["pull_request_id"])
            )
            if current is None or str(current["cycle_id"]) != cycle_id:
                raise ReviewError("Finding belongs to a stale review cycle")
            if ReviewCycleStatus(str(cycle["status"])) in {
                ReviewCycleStatus.SUPERSEDED,
                ReviewCycleStatus.HUMAN_APPROVED,
            }:
                raise ReviewError("Review cycle is no longer accepting findings")
            reader = connection.execute(
                """
                SELECT finding_ids_json, evidence_refs_json FROM review_readers
                WHERE cycle_id = ? AND concern = ?
                """,
                (cycle_id, concern.value),
            ).fetchone()
            if reader is None:
                raise ReviewError(f"No reader is configured for {concern.value}")
            finding_id = str(uuid4())
            timestamp = _now()
            connection.execute(
                """
                INSERT INTO review_findings(
                    finding_id, pull_request_id, cycle_id, concern, summary,
                    status, resolution, created_at, updated_at, evidence_refs_json
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
                """,
                (
                    finding_id,
                    str(cycle["pull_request_id"]),
                    cycle_id,
                    concern.value,
                    summary,
                    FindingStatus.OPEN.value,
                    timestamp,
                    timestamp,
                    json.dumps(refs),
                ),
            )
            finding_ids = list(_json_list(str(reader["finding_ids_json"])))
            finding_ids.append(finding_id)
            reader_refs = _evidence_refs(
                (*_json_list(str(reader["evidence_refs_json"])), *refs)
            )
            connection.execute(
                """
                UPDATE review_readers
                SET status = ?, finding_ids_json = ?, evidence_refs_json = ?,
                    updated_at = ?
                WHERE cycle_id = ? AND concern = ?
                """,
                (
                    ReaderStatus.FAIL.value,
                    json.dumps(finding_ids),
                    json.dumps(reader_refs),
                    timestamp,
                    cycle_id,
                    concern.value,
                ),
            )
            self._set_failed(connection, cycle_id)
        return self.store.snapshot(cycle_id)

    def resolve_finding(self, finding_id: str, resolution: str) -> ReviewSnapshot:
        finding_id = _required(finding_id, "finding id")
        resolution = _required(resolution, "resolution", 1_000)
        with self.store.transaction() as connection:
            finding = connection.execute(
                "SELECT * FROM review_findings WHERE finding_id = ?", (finding_id,)
            ).fetchone()
            if finding is None:
                raise ReviewError(f"Unknown review finding: {finding_id}")
            connection.execute(
                """
                UPDATE review_findings
                SET status = ?, resolution = ?, updated_at = ?
                WHERE finding_id = ?
                """,
                (
                    FindingStatus.RESOLVED.value,
                    resolution,
                    _now(),
                    finding_id,
                ),
            )
            current = self.store.current_cycle_row(
                connection, str(finding["pull_request_id"])
            )
            if current is None:
                raise ReviewError("No current review cycle exists for finding")
            cycle_id = str(current["cycle_id"])
        return self.store.snapshot(cycle_id)

    def approve_for_merge(
        self, cycle_id: str, reason: str, actor: str = "operator"
    ) -> ReviewSnapshot:
        actor = _required(actor, "approval actor", 100)
        reason = _required(reason, "approval reason", 1_000)
        with self.store.transaction() as connection:
            cycle = self.store.cycle_row(connection, cycle_id)
            if cycle is None:
                raise ReviewError(f"Unknown review cycle: {cycle_id}")
            current = self.store.current_cycle_row(
                connection, str(cycle["pull_request_id"])
            )
            if current is None or str(current["cycle_id"]) != cycle_id:
                raise ReviewError("Human approval belongs to a stale review cycle")
            connection.execute(
                """
                UPDATE review_cycles
                SET status = ?, human_approval = 1, required_action = NULL,
                    approval_actor = ?, approval_reason = ?, approval_at = ?,
                    updated_at = ?
                WHERE cycle_id = ?
                """,
                (
                    ReviewCycleStatus.HUMAN_APPROVED.value,
                    actor,
                    reason,
                    _now(),
                    _now(),
                    cycle_id,
                ),
            )
        return self.store.snapshot(cycle_id)

    def merge_handoff(self, pull_request_id: str, head_sha: str) -> MergeHandoff:
        pull_request_id = _required(pull_request_id, "pull request id")
        head_sha = _head_sha(head_sha)
        if self.provider is None:
            raise ReviewError("A pull-request provider is required for merge handoff")
        if self.store.current_cycle_id(pull_request_id) is None:
            raise ReviewError(f"No review cycle exists for {pull_request_id}")
        target = self._validated_provider_target(pull_request_id)
        with self.store.transaction() as connection:
            current = self.store.current_cycle_row(connection, pull_request_id)
            if current is None:
                raise ReviewError(f"No review cycle exists for {pull_request_id}")
            if (
                target.pull_request_id != pull_request_id
                or not target.ready
                or target.head_sha != head_sha
                or str(current["head_sha"]) != target.head_sha
            ):
                raise ReviewError(
                    "The pull request head has no current review authorization"
                )
            snapshot = self.store.snapshot_in_connection(
                connection, str(current["cycle_id"])
            )
            if not snapshot.merge_allowed:
                raise ReviewError(
                    snapshot.cycle.required_action
                    or "All required review readers must pass before merge handoff"
                )
            return MergeHandoff(
                pull_request_id,
                snapshot.cycle.cycle_id,
                head_sha,
                snapshot.cycle.human_approval,
                snapshot.cycle.approval_actor,
                snapshot.cycle.approval_reason,
                snapshot.cycle.approval_at,
            )

    def _set_failed(self, connection: sqlite3.Connection, cycle_id: str) -> None:
        connection.execute(
            """
            UPDATE review_cycles
            SET status = ?, required_action = ?, updated_at = ?
            WHERE cycle_id = ?
            """,
            (
                ReviewCycleStatus.FAILED.value,
                "Writer must resolve findings and start a new review cycle",
                _now(),
                cycle_id,
            ),
        )

    def _recompute_cycle(self, connection: sqlite3.Connection, cycle_id: str) -> None:
        statuses = [
            ReaderStatus(str(row["status"]))
            for row in connection.execute(
                "SELECT status FROM review_readers WHERE cycle_id = ?", (cycle_id,)
            ).fetchall()
        ]
        if any(status is ReaderStatus.FAIL for status in statuses):
            status = ReviewCycleStatus.FAILED
            required_action = (
                "Writer must resolve findings and start a new review cycle"
            )
        elif any(status is ReaderStatus.PENDING for status in statuses):
            status = ReviewCycleStatus.ACTIVE
            required_action = "Awaiting four specialized review readers"
        else:
            status = ReviewCycleStatus.PASSED
            required_action = None
        connection.execute(
            """
            UPDATE review_cycles
            SET status = ?, required_action = ?, updated_at = ?
            WHERE cycle_id = ?
            """,
            (status.value, required_action, _now(), cycle_id),
        )
