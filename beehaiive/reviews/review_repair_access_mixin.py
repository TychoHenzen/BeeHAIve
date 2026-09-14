from __future__ import annotations

from threading import Event, Thread
from typing import Any

from ..review import (
    ReviewRepairAttempt,
    ReviewRepairStatus,
)


class ReviewRepairAccessMixin:
    def dispatch(
        self: Any, cycle_id: str, finding_ids: tuple[str, ...], actor: str
    ) -> ReviewRepairAttempt:
        attempt, created = self.reviews.create_repair_attempt(
            cycle_id, finding_ids, actor
        )
        if not created and attempt.status is not ReviewRepairStatus.QUEUED:
            self._recover_attempt(attempt)
            return self.reviews.repair_attempt(attempt.attempt_id)
        self._start_worker(attempt.attempt_id)
        return self.reviews.repair_attempt(attempt.attempt_id)

    def _start_worker(self: Any, attempt_id: str) -> None:
        cancelled = Event()
        thread = Thread(
            target=self._run,
            args=(attempt_id, cancelled),
            name=f"beehaiive-review-repair-{attempt_id[:8]}",
            daemon=True,
        )
        with self._lock:
            if attempt_id in self._threads:
                return
            self._threads[attempt_id] = thread
            self._cancel_events[attempt_id] = cancelled
        try:
            thread.start()
        except RuntimeError as exc:
            self.reviews.store.finish_repair_attempt(
                attempt_id,
                ReviewRepairStatus.HUMAN_ACTION_REQUIRED,
                required_action=self._safe_text(str(exc), 1_000),
            )
            with self._lock:
                self._threads.pop(attempt_id, None)
                self._cancel_events.pop(attempt_id, None)

    def recover(self: Any) -> None:
        """Resume queued attempts and reconcile attempts left by a stopped worker."""

        for attempt in self.reviews.store.pending_repair_attempts():
            self._recover_attempt(attempt)

    def get(self: Any, attempt_id: str) -> ReviewRepairAttempt:
        self._recover_attempt(self.reviews.repair_attempt(attempt_id))
        return self.reviews.repair_attempt(attempt_id)

    def cancel(self: Any, attempt_id: str, actor: str) -> ReviewRepairAttempt:
        attempt = self.reviews.cancel_repair_attempt(attempt_id, actor)
        if (
            attempt.status is ReviewRepairStatus.RUNNING
            and attempt.cancellation_requested
        ):
            with self._lock:
                cancelled = self._cancel_events.get(attempt_id)
            if cancelled is not None:
                cancelled.set()
            self.agent.cancel(attempt_id)
        return self.reviews.repair_attempt(attempt_id)

    def wait(
        self: Any, attempt_id: str, timeout: float | None = None
    ) -> ReviewRepairAttempt:
        """Wait for one local worker, primarily for deterministic tests."""

        with self._lock:
            thread = self._threads.get(attempt_id)
        if thread is not None:
            thread.join(timeout)
        return self.reviews.repair_attempt(attempt_id)
