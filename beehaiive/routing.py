"""Configurable model routing with durable, bounded escalation state."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from threading import Lock, RLock
from typing import Protocol


class RoutingError(RuntimeError):
    """Raised when a routing transition cannot be applied."""


class ModelTier(StrEnum):
    """Available model tiers, ordered from routine work to human review."""

    LUNA = "luna"
    TERRA = "terra"
    SOL = "sol"
    ASTRA = "astra"
    HUMAN = "human"


class AttemptOutcome(StrEnum):
    """Result reported for one model attempt."""

    FAILURE = "failure"
    RETRY = "retry"
    SUCCESS = "success"


@dataclass(frozen=True, slots=True)
class ModelExecution:
    """Measured result returned by one configured model invocation."""

    outcome: AttemptOutcome | str
    input_tokens: int = 0
    output_tokens: int = 0
    failure_context: str = ""
    recursive_spawn_depth: int = 0

    def __post_init__(self) -> None:
        if self.input_tokens < 0 or self.output_tokens < 0:
            raise ValueError("Model token usage must not be negative")
        if self.recursive_spawn_depth < 0:
            raise ValueError("Model recursive spawn depth must not be negative")


class RoutingStatus(StrEnum):
    """Lifecycle state of one active problem."""

    ACTIVE = "active"
    RESOLVED = "resolved"
    HUMAN_HANDOFF = "human_handoff"


@dataclass(frozen=True, slots=True)
class ModelSpec:
    """A configured model and its estimated per-million-token prices."""

    tier: ModelTier
    model: str
    input_cost_per_million: float = 0.0
    output_cost_per_million: float = 0.0

    def __post_init__(self) -> None:
        if not self.model.strip():
            raise ValueError("model must not be empty")
        if self.input_cost_per_million < 0 or self.output_cost_per_million < 0:
            raise ValueError("model costs must not be negative")

    def estimate_cost(self, input_tokens: int, output_tokens: int) -> float:
        return round(
            input_tokens * self.input_cost_per_million / 1_000_000
            + output_tokens * self.output_cost_per_million / 1_000_000,
            8,
        )


@dataclass(frozen=True, slots=True)
class RoutingLimits:
    """Hard limits that prevent an unresolved problem from running forever."""

    max_rounds: int = 8
    max_tokens: int = 12_000
    max_recursive_spawn_depth: int = 2
    max_bounces: int = 6

    def __post_init__(self) -> None:
        if self.max_rounds <= 0:
            raise ValueError("max_rounds must be positive")
        if self.max_tokens <= 0:
            raise ValueError("max_tokens must be positive")
        if self.max_recursive_spawn_depth < 0:
            raise ValueError("max_recursive_spawn_depth must not be negative")
        if self.max_bounces <= 0:
            raise ValueError("max_bounces must be positive")


def _default_writer() -> ModelSpec:
    return ModelSpec(ModelTier.LUNA, "luna", 0.20, 1.20)


def _default_triage() -> tuple[ModelSpec, ...]:
    return (
        ModelSpec(ModelTier.TERRA, "terra", 0.50, 2.00),
        ModelSpec(ModelTier.SOL, "sol", 1.00, 4.00),
        ModelSpec(ModelTier.ASTRA, "astra", 2.00, 8.00),
    )


@dataclass(frozen=True, slots=True)
class RoutingConfig:
    """Model tiers and limits used by one router instance."""

    writer: ModelSpec = field(default_factory=_default_writer)
    triage: tuple[ModelSpec, ...] = field(default_factory=_default_triage)
    limits: RoutingLimits = field(default_factory=RoutingLimits)

    def __post_init__(self) -> None:
        if self.writer.tier is not ModelTier.LUNA:
            raise ValueError("writer must use the Luna tier")
        triage_tiers = [spec.tier for spec in self.triage]
        if not triage_tiers:
            raise ValueError("at least one triage tier is required")
        if any(tier in {ModelTier.LUNA, ModelTier.HUMAN} for tier in triage_tiers):
            raise ValueError("triage tiers must not include Luna or Human")
        if len(set(triage_tiers)) != len(triage_tiers):
            raise ValueError("triage tiers must be unique")

    def spec_for(self, tier: ModelTier) -> ModelSpec:
        if tier is self.writer.tier:
            return self.writer
        for spec in self.triage:
            if spec.tier is tier:
                return spec
        if tier is ModelTier.HUMAN:
            return ModelSpec(ModelTier.HUMAN, "human")
        raise RoutingError(f"No model is configured for tier {tier.value}")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _compact_context(value: str, limit: int = 280) -> str:
    compacted = " ".join(value.split())
    if len(compacted) <= limit:
        return compacted
    return compacted[: limit - 3].rstrip() + "..."


class _ExecutionLockEntry:
    def __init__(self) -> None:
        self.lock = Lock()
        self.users = 0


class _ExecutionLockManager:
    def __init__(self) -> None:
        self._entries: dict[str, _ExecutionLockEntry] = {}
        self._guard = Lock()

    @contextmanager
    def acquire(self, key: str) -> Generator[None]:
        with self._guard:
            entry = self._entries.get(key)
            if entry is None:
                entry = _ExecutionLockEntry()
                self._entries[key] = entry
            entry.users += 1
        entry.lock.acquire()
        try:
            yield
        finally:
            entry.lock.release()
            with self._guard:
                entry.users -= 1
                if entry.users == 0 and self._entries.get(key) is entry:
                    del self._entries[key]


@dataclass(frozen=True, slots=True)
class RoutingState:
    """Persisted counters and the next route for one problem."""

    problem_id: str
    status: RoutingStatus
    current_tier: ModelTier
    triage_index: int
    consecutive_failures: int
    bounce_count: int
    round: int
    total_tokens: int
    total_cost: float
    recursive_spawn_depth: int
    last_failure_context: str | None
    required_action: str | None
    next_reason: str
    created_at: str
    updated_at: str

    def as_dict(self) -> dict[str, object]:
        return {
            "problem_id": self.problem_id,
            "status": self.status.value,
            "current_tier": self.current_tier.value,
            "triage_index": self.triage_index,
            "consecutive_failures": self.consecutive_failures,
            "bounce_count": self.bounce_count,
            "round": self.round,
            "total_tokens": self.total_tokens,
            "total_cost": self.total_cost,
            "recursive_spawn_depth": self.recursive_spawn_depth,
            "last_failure_context": self.last_failure_context,
            "required_action": self.required_action,
            "next_reason": self.next_reason,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class RoutingAttempt:
    """One persisted model call and its accounting data."""

    attempt_id: int | None
    problem_id: str
    round: int
    model: str
    tier: ModelTier
    reason: str
    outcome: AttemptOutcome
    input_tokens: int
    output_tokens: int
    total_tokens: int
    estimated_cost: float
    bounce_count: int
    recursive_spawn_depth: int
    failure_context: str | None
    created_at: str

    def as_dict(self) -> dict[str, object]:
        return {
            "attempt_id": self.attempt_id,
            "problem_id": self.problem_id,
            "round": self.round,
            "model": self.model,
            "tier": self.tier.value,
            "reason": self.reason,
            "outcome": self.outcome.value,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "estimated_cost": self.estimated_cost,
            "bounce_count": self.bounce_count,
            "recursive_spawn_depth": self.recursive_spawn_depth,
            "failure_context": self.failure_context,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    """The next model route and any operator action required."""

    problem_id: str
    status: RoutingStatus
    tier: ModelTier
    model: str
    reason: str
    failure_context: str | None
    requires_human: bool
    round: int
    consecutive_failures: int
    bounce_count: int
    total_tokens: int
    total_cost: float
    remaining_rounds: int
    remaining_tokens: int
    remaining_recursive_spawn_depth: int
    remaining_bounces: int

    def as_dict(self) -> dict[str, object]:
        return {
            "problem_id": self.problem_id,
            "status": self.status.value,
            "tier": self.tier.value,
            "model": self.model,
            "reason": self.reason,
            "failure_context": self.failure_context,
            "requires_human": self.requires_human,
            "round": self.round,
            "consecutive_failures": self.consecutive_failures,
            "bounce_count": self.bounce_count,
            "total_tokens": self.total_tokens,
            "total_cost": self.total_cost,
            "remaining_rounds": self.remaining_rounds,
            "remaining_tokens": self.remaining_tokens,
            "remaining_recursive_spawn_depth": self.remaining_recursive_spawn_depth,
            "remaining_bounces": self.remaining_bounces,
        }


@dataclass(frozen=True, slots=True)
class RoutingResult:
    """API-ready view of one routing transition."""

    state: RoutingState
    decision: RoutingDecision
    attempt: RoutingAttempt | None
    attempts: tuple[RoutingAttempt, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "state": self.state.as_dict(),
            "decision": self.decision.as_dict(),
            "attempt": None if self.attempt is None else self.attempt.as_dict(),
            "attempts": [attempt.as_dict() for attempt in self.attempts],
        }


class ModelExecutor(Protocol):
    """Boundary for invoking the model selected by the router."""

    def execute(self, spec: ModelSpec, decision: RoutingDecision) -> ModelExecution: ...


class RoutingStore:
    """Thread-safe SQLite persistence for routing state and attempt history."""

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

    def _initialize(self) -> None:
        with self._lock:
            self._connection.executescript(
                """
                PRAGMA foreign_keys = ON;

                CREATE TABLE IF NOT EXISTS routing_problems (
                    problem_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    current_tier TEXT NOT NULL,
                    triage_index INTEGER NOT NULL,
                    consecutive_failures INTEGER NOT NULL,
                    bounce_count INTEGER NOT NULL,
                    round INTEGER NOT NULL,
                    total_tokens INTEGER NOT NULL,
                    total_cost REAL NOT NULL,
                    recursive_spawn_depth INTEGER NOT NULL,
                    last_failure_context TEXT,
                    required_action TEXT,
                    next_reason TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS routing_attempts (
                    attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    problem_id TEXT NOT NULL,
                    round INTEGER NOT NULL,
                    model TEXT NOT NULL,
                    tier TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL,
                    output_tokens INTEGER NOT NULL,
                    total_tokens INTEGER NOT NULL,
                    estimated_cost REAL NOT NULL,
                    bounce_count INTEGER NOT NULL,
                    recursive_spawn_depth INTEGER NOT NULL,
                    failure_context TEXT,
                    created_at TEXT NOT NULL,
                    transition_id TEXT,
                    FOREIGN KEY (problem_id) REFERENCES routing_problems(problem_id)
                );
                """
            )
            attempt_columns = {
                str(row["name"])
                for row in self._connection.execute(
                    "PRAGMA table_info(routing_attempts)"
                ).fetchall()
            }
            if "transition_id" not in attempt_columns:
                self._connection.execute(
                    "ALTER TABLE routing_attempts ADD COLUMN transition_id TEXT"
                )
            self._connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS routing_attempt_transition
                ON routing_attempts(transition_id)
                WHERE transition_id IS NOT NULL
                """
            )

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

    def create_problem(self, state: RoutingState) -> None:
        try:
            with self._transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO routing_problems(
                        problem_id, status, current_tier, triage_index,
                        consecutive_failures, bounce_count, round, total_tokens,
                        total_cost, recursive_spawn_depth, last_failure_context,
                        required_action, next_reason, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    _state_values(state),
                )
        except sqlite3.IntegrityError as exc:
            raise RoutingError(
                f"Routing problem {state.problem_id} already exists"
            ) from exc

    def get_problem(self, problem_id: str) -> RoutingState | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM routing_problems WHERE problem_id = ?",
                (problem_id,),
            ).fetchone()
        return None if row is None else _state_from_row(row)

    def get_attempts(self, problem_id: str) -> tuple[RoutingAttempt, ...]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM routing_attempts
                WHERE problem_id = ?
                ORDER BY attempt_id
                """,
                (problem_id,),
            ).fetchall()
        return tuple(_attempt_from_row(row) for row in rows)

    def has_transition(self, transition_id: str) -> bool:
        with self._lock:
            row = self._connection.execute(
                "SELECT 1 FROM routing_attempts WHERE transition_id = ?",
                (transition_id,),
            ).fetchone()
        return row is not None

    def save_transition(
        self,
        problem_id: str,
        expected_round: int,
        state: RoutingState,
        attempt: RoutingAttempt,
        transition_id: str | None = None,
    ) -> tuple[RoutingState, RoutingAttempt]:
        with self._transaction() as connection:
            if transition_id is not None:
                existing_attempt = connection.execute(
                    "SELECT * FROM routing_attempts WHERE transition_id = ?",
                    (transition_id,),
                ).fetchone()
                if existing_attempt is not None:
                    if str(existing_attempt["problem_id"]) != problem_id:
                        raise RoutingError(
                            "Routing transition belongs to another problem"
                        )
                    existing_state = connection.execute(
                        "SELECT * FROM routing_problems WHERE problem_id = ?",
                        (problem_id,),
                    ).fetchone()
                    if existing_state is None:
                        raise RoutingError(f"Unknown routing problem: {problem_id}")
                    return _state_from_row(existing_state), _attempt_from_row(
                        existing_attempt
                    )
            current = connection.execute(
                "SELECT round, status FROM routing_problems WHERE problem_id = ?",
                (problem_id,),
            ).fetchone()
            if current is None:
                raise RoutingError(f"Unknown routing problem: {problem_id}")
            if int(current["round"]) != expected_round:
                raise RoutingError("Routing problem changed during the transition")
            connection.execute(
                """
                UPDATE routing_problems
                SET status = ?, current_tier = ?, triage_index = ?,
                    consecutive_failures = ?, bounce_count = ?, round = ?,
                    total_tokens = ?, total_cost = ?,
                    recursive_spawn_depth = ?, last_failure_context = ?,
                    required_action = ?, next_reason = ?, updated_at = ?
                WHERE problem_id = ?
                """,
                (
                    state.status.value,
                    state.current_tier.value,
                    state.triage_index,
                    state.consecutive_failures,
                    state.bounce_count,
                    state.round,
                    state.total_tokens,
                    state.total_cost,
                    state.recursive_spawn_depth,
                    state.last_failure_context,
                    state.required_action,
                    state.next_reason,
                    state.updated_at,
                    problem_id,
                ),
            )
            cursor = connection.execute(
                """
                INSERT INTO routing_attempts(
                    problem_id, round, model, tier, reason, outcome,
                    input_tokens, output_tokens, total_tokens, estimated_cost,
                    bounce_count, recursive_spawn_depth, failure_context,
                    created_at, transition_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt.problem_id,
                    attempt.round,
                    attempt.model,
                    attempt.tier.value,
                    attempt.reason,
                    attempt.outcome.value,
                    attempt.input_tokens,
                    attempt.output_tokens,
                    attempt.total_tokens,
                    attempt.estimated_cost,
                    attempt.bounce_count,
                    attempt.recursive_spawn_depth,
                    attempt.failure_context,
                    attempt.created_at,
                    transition_id,
                ),
            )
            attempt_id = cursor.lastrowid
            if attempt_id is None:  # pragma: no cover - SQLite assigns row ids
                raise RoutingError("Routing attempt was not assigned an id")
            saved_state = _state_from_row(
                connection.execute(
                    "SELECT * FROM routing_problems WHERE problem_id = ?",
                    (problem_id,),
                ).fetchone()
            )
        return saved_state, replace(attempt, attempt_id=int(attempt_id))


def _state_values(state: RoutingState) -> tuple[object, ...]:
    return (
        state.problem_id,
        state.status.value,
        state.current_tier.value,
        state.triage_index,
        state.consecutive_failures,
        state.bounce_count,
        state.round,
        state.total_tokens,
        state.total_cost,
        state.recursive_spawn_depth,
        state.last_failure_context,
        state.required_action,
        state.next_reason,
        state.created_at,
        state.updated_at,
    )


def _state_from_row(row: sqlite3.Row) -> RoutingState:
    return RoutingState(
        problem_id=str(row["problem_id"]),
        status=RoutingStatus(str(row["status"])),
        current_tier=ModelTier(str(row["current_tier"])),
        triage_index=int(row["triage_index"]),
        consecutive_failures=int(row["consecutive_failures"]),
        bounce_count=int(row["bounce_count"]),
        round=int(row["round"]),
        total_tokens=int(row["total_tokens"]),
        total_cost=float(row["total_cost"]),
        recursive_spawn_depth=int(row["recursive_spawn_depth"]),
        last_failure_context=row["last_failure_context"],
        required_action=row["required_action"],
        next_reason=str(row["next_reason"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _attempt_from_row(row: sqlite3.Row) -> RoutingAttempt:
    return RoutingAttempt(
        attempt_id=int(row["attempt_id"]),
        problem_id=str(row["problem_id"]),
        round=int(row["round"]),
        model=str(row["model"]),
        tier=ModelTier(str(row["tier"])),
        reason=str(row["reason"]),
        outcome=AttemptOutcome(str(row["outcome"])),
        input_tokens=int(row["input_tokens"]),
        output_tokens=int(row["output_tokens"]),
        total_tokens=int(row["total_tokens"]),
        estimated_cost=float(row["estimated_cost"]),
        bounce_count=int(row["bounce_count"]),
        recursive_spawn_depth=int(row["recursive_spawn_depth"]),
        failure_context=row["failure_context"],
        created_at=str(row["created_at"]),
    )


class ModelRouter:
    """Route writer work, escalate failures, and stop at explicit limits."""

    def __init__(
        self, store: RoutingStore, config: RoutingConfig | None = None
    ) -> None:
        self.store = store
        self.config = config or RoutingConfig()
        self._execution_locks = _ExecutionLockManager()

    def begin(self, problem_id: str) -> RoutingResult:
        normalized_id = _problem_id(problem_id)
        if self.store.get_problem(normalized_id) is not None:
            raise RoutingError(f"Routing problem {normalized_id} already exists")
        timestamp = _now()
        state = RoutingState(
            problem_id=normalized_id,
            status=RoutingStatus.ACTIVE,
            current_tier=self.config.writer.tier,
            triage_index=0,
            consecutive_failures=0,
            bounce_count=0,
            round=0,
            total_tokens=0,
            total_cost=0.0,
            recursive_spawn_depth=0,
            last_failure_context=None,
            required_action=None,
            next_reason="routine",
            created_at=timestamp,
            updated_at=timestamp,
        )
        self.store.create_problem(state)
        return RoutingResult(state, self._decision(state), None, ())

    def snapshot(self, problem_id: str) -> RoutingResult:
        normalized_id = _problem_id(problem_id)
        state = self.store.get_problem(normalized_id)
        if state is None:
            raise RoutingError(f"Unknown routing problem: {normalized_id}")
        return RoutingResult(
            state,
            self._decision(state),
            None,
            self.store.get_attempts(normalized_id),
        )

    @contextmanager
    def coordinate(self, problem_id: str) -> Generator[None]:
        """Serialize external run transitions with model execution."""

        normalized_id = _problem_id(problem_id)
        with self._execution_locks.acquire(normalized_id):
            yield

    def execute(
        self,
        problem_id: str,
        executor: ModelExecutor,
        before_record: Callable[[], None] | None = None,
    ) -> RoutingResult:
        """Invoke the selected model and persist its measured routing result."""

        normalized_id = _problem_id(problem_id)
        with self.coordinate(normalized_id):
            current = self.snapshot(normalized_id)
            if current.state.status is not RoutingStatus.ACTIVE:
                raise RoutingError(
                    f"Routing problem {current.state.problem_id} is already "
                    f"{current.state.status.value}"
                )
            limit_reason = self._limit_reason(
                current.state.round,
                current.state.total_tokens,
                current.state.recursive_spawn_depth,
                current.state.bounce_count,
            )
            if limit_reason is not None:
                if before_record is not None:
                    before_record()
                return self.record(
                    normalized_id,
                    AttemptOutcome.FAILURE,
                    failure_context=f"Model invocation skipped: {limit_reason}",
                    force_human_reason=limit_reason,
                )
            try:
                execution = executor.execute(
                    self.config.spec_for(current.state.current_tier), current.decision
                )
            except Exception as exc:
                execution = _failed_model_execution(exc)
            if before_record is not None:
                before_record()
            usage_violation = self._usage_violation(current, execution)
            if usage_violation is not None:
                remaining_tokens = current.decision.remaining_tokens
                bounded_input = min(execution.input_tokens, remaining_tokens)
                bounded_output = min(
                    execution.output_tokens, remaining_tokens - bounded_input
                )
                return self.record(
                    normalized_id,
                    AttemptOutcome.FAILURE,
                    input_tokens=bounded_input,
                    output_tokens=bounded_output,
                    failure_context=usage_violation,
                    recursive_spawn_depth=min(
                        execution.recursive_spawn_depth,
                        self.config.limits.max_recursive_spawn_depth,
                    ),
                    force_human_reason=usage_violation,
                )
            return self.record(
                normalized_id,
                execution.outcome,
                input_tokens=execution.input_tokens,
                output_tokens=execution.output_tokens,
                failure_context=execution.failure_context,
                recursive_spawn_depth=execution.recursive_spawn_depth,
            )

    def record(
        self,
        problem_id: str,
        outcome: AttemptOutcome | str,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        failure_context: str = "",
        recursive_spawn_depth: int = 0,
        transition_id: str | None = None,
        force_human_reason: str | None = None,
    ) -> RoutingResult:
        normalized_id = _problem_id(problem_id)
        try:
            resolved_outcome = AttemptOutcome(outcome)
        except ValueError as exc:
            raise RoutingError(f"Unknown attempt outcome: {outcome}") from exc
        if input_tokens < 0 or output_tokens < 0:
            raise RoutingError("Token usage must not be negative")
        if recursive_spawn_depth < 0:
            raise RoutingError("Recursive spawn depth must not be negative")
        if force_human_reason is not None and not force_human_reason.strip():
            raise RoutingError("Human handoff reason must not be empty")
        state = self.store.get_problem(normalized_id)
        if state is None:
            raise RoutingError(f"Unknown routing problem: {normalized_id}")
        can_resolve_human_handoff = (
            state.status is RoutingStatus.HUMAN_HANDOFF
            and resolved_outcome is AttemptOutcome.SUCCESS
        )
        if state.status is not RoutingStatus.ACTIVE and not can_resolve_human_handoff:
            raise RoutingError(
                f"Routing problem {normalized_id} is already {state.status.value}"
            )

        context = _compact_context(failure_context)
        if (
            resolved_outcome in {AttemptOutcome.FAILURE, AttemptOutcome.RETRY}
            and not context
        ):
            raise RoutingError("Failure context is required for an unresolved attempt")
        if resolved_outcome is AttemptOutcome.RETRY and state.current_tier in {
            self.config.writer.tier,
            ModelTier.HUMAN,
        }:
            raise RoutingError("Only a triage tier can return writer retry context")

        spec = self.config.spec_for(state.current_tier)
        attempt_round = state.round + 1
        attempt_tokens = input_tokens + output_tokens
        total_tokens = state.total_tokens + attempt_tokens
        total_depth = max(state.recursive_spawn_depth, recursive_spawn_depth)
        consecutive_failures = (
            state.consecutive_failures + 1
            if resolved_outcome is AttemptOutcome.FAILURE
            else state.consecutive_failures
        )
        attempt_bounces = (
            state.bounce_count + 1
            if resolved_outcome is not AttemptOutcome.SUCCESS
            else state.bounce_count
        )
        attempt = RoutingAttempt(
            attempt_id=None,
            problem_id=normalized_id,
            round=attempt_round,
            model=spec.model,
            tier=state.current_tier,
            reason=state.next_reason,
            outcome=resolved_outcome,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=attempt_tokens,
            estimated_cost=spec.estimate_cost(input_tokens, output_tokens),
            bounce_count=attempt_bounces,
            recursive_spawn_depth=recursive_spawn_depth,
            failure_context=state.last_failure_context,
            created_at=_now(),
        )
        next_state = self._next_state(
            state,
            resolved_outcome,
            context,
            attempt_round,
            total_tokens,
            total_depth,
            attempt_bounces,
            attempt.estimated_cost,
            consecutive_failures,
            force_human_reason,
        )
        saved_state, saved_attempt = self.store.save_transition(
            normalized_id, state.round, next_state, attempt, transition_id
        )
        return RoutingResult(
            saved_state,
            self._decision(saved_state),
            saved_attempt,
            self.store.get_attempts(normalized_id),
        )

    def _next_state(
        self,
        state: RoutingState,
        outcome: AttemptOutcome,
        context: str,
        attempt_round: int,
        total_tokens: int,
        total_depth: int,
        attempt_bounces: int,
        attempt_cost: float,
        consecutive_failures: int,
        force_human_reason: str | None = None,
    ) -> RoutingState:
        total_cost = round(state.total_cost + attempt_cost, 8)
        if force_human_reason is not None:
            return self._human_state(
                state,
                attempt_round,
                total_tokens,
                total_cost,
                total_depth,
                attempt_bounces,
                force_human_reason,
                context,
                consecutive_failures,
            )
        limit_reason = self._limit_reason(
            attempt_round, total_tokens, total_depth, attempt_bounces
        )
        if outcome is AttemptOutcome.SUCCESS:
            if state.current_tier is not ModelTier.HUMAN and limit_reason is not None:
                return self._human_state(
                    state,
                    attempt_round,
                    total_tokens,
                    total_cost,
                    total_depth,
                    attempt_bounces,
                    limit_reason,
                    context,
                    consecutive_failures,
                )
            return replace(
                state,
                status=RoutingStatus.RESOLVED,
                current_tier=self.config.writer.tier,
                triage_index=0,
                consecutive_failures=0,
                bounce_count=0,
                round=attempt_round,
                total_tokens=total_tokens,
                total_cost=total_cost,
                recursive_spawn_depth=total_depth,
                last_failure_context=None,
                required_action=None,
                next_reason="reset after successful fix",
                updated_at=_now(),
            )

        if limit_reason is not None:
            return self._human_state(
                state,
                attempt_round,
                total_tokens,
                total_cost,
                total_depth,
                attempt_bounces,
                limit_reason,
                context,
                consecutive_failures,
            )

        if outcome is AttemptOutcome.RETRY:
            return replace(
                state,
                current_tier=self.config.writer.tier,
                round=attempt_round,
                total_tokens=total_tokens,
                total_cost=total_cost,
                recursive_spawn_depth=total_depth,
                bounce_count=attempt_bounces,
                last_failure_context=context,
                required_action=None,
                next_reason="triage context returned to writer",
                updated_at=_now(),
            )

        next_tier, next_index = self._next_triage_tier(state)
        if next_tier is None:
            return self._human_state(
                state,
                attempt_round,
                total_tokens,
                total_cost,
                total_depth,
                attempt_bounces,
                "All configured triage tiers failed",
                context,
                consecutive_failures,
            )
        return replace(
            state,
            current_tier=next_tier,
            triage_index=next_index,
            consecutive_failures=consecutive_failures,
            round=attempt_round,
            total_tokens=total_tokens,
            total_cost=total_cost,
            recursive_spawn_depth=total_depth,
            bounce_count=attempt_bounces,
            last_failure_context=context,
            required_action=None,
            next_reason=f"escalation after {state.current_tier.value} failure",
            updated_at=_now(),
        )

    def _next_triage_tier(self, state: RoutingState) -> tuple[ModelTier | None, int]:
        if state.triage_index >= len(self.config.triage):
            return None, state.triage_index
        next_spec = self.config.triage[state.triage_index]
        return next_spec.tier, state.triage_index + 1

    def _limit_reason(
        self, round_number: int, total_tokens: int, depth: int, bounces: int
    ) -> str | None:
        limits = self.config.limits
        if round_number >= limits.max_rounds:
            return f"Maximum routing rounds reached ({limits.max_rounds})"
        if total_tokens >= limits.max_tokens:
            return f"Token ceiling reached ({limits.max_tokens})"
        if depth > limits.max_recursive_spawn_depth:
            return f"Recursive spawn limit reached ({limits.max_recursive_spawn_depth})"
        if bounces >= limits.max_bounces:
            return f"Maximum routing bounces reached ({limits.max_bounces})"
        return None

    @staticmethod
    def _human_state(
        state: RoutingState,
        round_number: int,
        total_tokens: int,
        total_cost: float,
        depth: int,
        bounces: int,
        reason: str,
        failure_context: str,
        consecutive_failures: int,
    ) -> RoutingState:
        return replace(
            state,
            status=RoutingStatus.HUMAN_HANDOFF,
            current_tier=ModelTier.HUMAN,
            round=round_number,
            total_tokens=total_tokens,
            total_cost=total_cost,
            recursive_spawn_depth=depth,
            bounce_count=bounces,
            consecutive_failures=consecutive_failures,
            last_failure_context=failure_context,
            required_action=f"Human action required: {reason}",
            next_reason="human handoff",
            updated_at=_now(),
        )

    def _decision(self, state: RoutingState) -> RoutingDecision:
        spec = self.config.spec_for(state.current_tier)
        limits = self.config.limits
        return RoutingDecision(
            problem_id=state.problem_id,
            status=state.status,
            tier=state.current_tier,
            model=spec.model,
            reason=state.next_reason,
            failure_context=state.last_failure_context,
            requires_human=state.status is RoutingStatus.HUMAN_HANDOFF,
            round=state.round,
            consecutive_failures=state.consecutive_failures,
            bounce_count=state.bounce_count,
            total_tokens=state.total_tokens,
            total_cost=state.total_cost,
            remaining_rounds=max(0, limits.max_rounds - state.round),
            remaining_tokens=max(0, limits.max_tokens - state.total_tokens),
            remaining_recursive_spawn_depth=max(
                0, limits.max_recursive_spawn_depth - state.recursive_spawn_depth
            ),
            remaining_bounces=max(0, limits.max_bounces - state.bounce_count),
        )

    def handoff_limit_reason(self, problem_id: str) -> str | None:
        """Return a limit that blocks resolving a handoff without a model call."""

        state = self.store.get_problem(_problem_id(problem_id))
        if state is None:
            raise RoutingError(f"Unknown routing problem: {problem_id}")
        if state.status is not RoutingStatus.ACTIVE:
            return None
        return self._limit_reason(
            state.round + 1,
            state.total_tokens,
            state.recursive_spawn_depth,
            state.bounce_count,
        )

    def _usage_violation(
        self, current: RoutingResult, execution: ModelExecution
    ) -> str | None:
        if (
            execution.input_tokens + execution.output_tokens
            > current.decision.remaining_tokens
        ):
            return (
                "Token ceiling reached. Model reported more tokens than the "
                f"remaining routing budget ({current.decision.remaining_tokens})"
            )
        if (
            execution.recursive_spawn_depth
            > self.config.limits.max_recursive_spawn_depth
        ):
            return (
                "Recursive spawn limit reached. Model reported depth "
                f"{execution.recursive_spawn_depth} above the configured limit "
                f"({self.config.limits.max_recursive_spawn_depth})"
            )
        return None


def _failed_model_execution(error: Exception) -> ModelExecution:
    def usage_value(name: str) -> int:
        value = getattr(error, name, 0)
        return value if isinstance(value, int) and value >= 0 else 0

    failure_context = getattr(error, "failure_context", "")
    if not isinstance(failure_context, str) or not failure_context.strip():
        failure_context = f"Model execution failed: {error or type(error).__name__}"
    return ModelExecution(
        AttemptOutcome.FAILURE,
        input_tokens=usage_value("input_tokens"),
        output_tokens=usage_value("output_tokens"),
        failure_context=failure_context,
        recursive_spawn_depth=usage_value("recursive_spawn_depth"),
    )


def _problem_id(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise RoutingError("A routing problem id is required")
    if len(normalized) > 200:
        raise RoutingError("A routing problem id must be 200 characters or fewer")
    return normalized
