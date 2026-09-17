from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final


class FixtureStage(StrEnum):
    CREATE = "create"
    REFINE = "refine"
    DECOMPOSE = "decompose"
    IMPLEMENT = "implementation"
    CHECKS = "checks"
    DRAFT_PULL_REQUEST = "draft pull request"
    REVIEW = "review"
    REPAIR = "repair"
    RE_REVIEW = "re-review"
    CONFLICT = "conflict handling"
    MERGE = "merge"
    ISSUE_COMPLETION = "issue completion"
    PROJECT_COMPLETION = "project completion"
    CLEANUP = "cleanup"


STAGES: Final[tuple[FixtureStage, ...]] = tuple(FixtureStage)
FAILURE_STAGES: Final[frozenset[str]] = frozenset(
    {"failed checks", "unknown provider", "stale head", "cancelled", "cleanup error"}
)


@dataclass(frozen=True)
class FixtureEvent:
    stage: str
    outcome: str
    detail: str


@dataclass
class DisposableDeliveryFixture:
    """Deterministic double for the complete local delivery lifecycle."""

    failure: str | None = None
    events: list[FixtureEvent] = field(default_factory=lambda: list[FixtureEvent]())
    artifacts: dict[str, str] = field(default_factory=lambda: dict[str, str]())
    attempts: dict[str, int] = field(default_factory=lambda: dict[str, int]())
    completed: bool = False
    cleaned: bool = False

    def run(self) -> DisposableDeliveryFixture:
        if self.failure is not None and self.failure not in FAILURE_STAGES:
            raise ValueError(f"Unsupported fixture failure: {self.failure}")
        for stage in STAGES:
            if self.failure == "cleanup error" and stage is FixtureStage.CLEANUP:
                self._record(stage, "blocked", "cleanup could not remove fixture")
                break
            if self.failure == "cancelled" and stage is FixtureStage.REPAIR:
                self._record(
                    stage, "recoverable", "operator cancellation preserved state"
                )
                break
            if self.failure == "failed checks" and stage is FixtureStage.CHECKS:
                self._record(stage, "blocked", "required check failed")
                break
            if self.failure == "unknown provider" and stage is FixtureStage.REVIEW:
                self._record(stage, "blocked", "provider result was unknown")
                break
            if self.failure == "stale head" and stage is FixtureStage.RE_REVIEW:
                self._record(stage, "blocked", "pull request head changed")
                break
            self._run_stage(stage)
        return self

    def retry(self) -> DisposableDeliveryFixture:
        """Record a retry without duplicating local artifacts or events."""

        self.attempts["run"] = self.attempts.get("run", 0) + 1
        return self

    def report(self) -> dict[str, object]:
        return {
            "evidence": "fixture",
            "commands": [event.detail for event in self.events],
            "outcomes": [event.outcome for event in self.events],
            "completed": self.completed,
            "cleaned": self.cleaned,
            "artifacts": dict(self.artifacts),
        }

    def _run_stage(self, stage: FixtureStage) -> None:
        self._record(stage, "passed", f"fixture:{stage.value}")
        if stage is FixtureStage.CREATE:
            self.artifacts["issue"] = "fixture-issue-1"
        elif stage is FixtureStage.DECOMPOSE:
            self.artifacts["subtask"] = "fixture-subtask-1"
        elif stage is FixtureStage.IMPLEMENT:
            self.artifacts.update(
                {
                    "worktree": "leased-worktree-1",
                    "branch": "codex/fixture-1",
                    "commit": "a" * 40,
                }
            )
        elif stage is FixtureStage.DRAFT_PULL_REQUEST:
            self.artifacts["pull_request"] = "fixture-pr-1"
        elif stage is FixtureStage.REPAIR:
            self.artifacts["repair_commit"] = "b" * 40
        elif stage is FixtureStage.CLEANUP:
            self.cleaned = True
            self.completed = True

    def _record(self, stage: FixtureStage, outcome: str, detail: str) -> None:
        self.events.append(FixtureEvent(stage.value, outcome, detail))


def run_disposable_delivery_fixture(
    failure: str | None = None,
) -> DisposableDeliveryFixture:
    return DisposableDeliveryFixture(failure=failure).run()
