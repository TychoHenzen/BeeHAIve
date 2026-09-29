from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import UTC, datetime

from .database import SnapshotDatabase
from .models import ProjectColumn, ProjectSnapshot
from .project import ProjectProvider
from .rest import RateLimitError


class ProjectSnapshotService:
    def __init__(
        self,
        provider: ProjectProvider,
        database: SnapshotDatabase,
        minimum_refresh_seconds: float = 60.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.provider = provider
        self.database = database
        self.minimum_refresh_seconds = max(0.0, minimum_refresh_seconds)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._condition = threading.Condition()
        self._refreshing = False
        self._last_error: BaseException | None = None

    def get_snapshot(self) -> ProjectSnapshot:
        return self._get_or_refresh()

    def request_refresh(self) -> ProjectSnapshot:
        return self._get_or_refresh()

    def _get_or_refresh(self) -> ProjectSnapshot:
        existing = self.database.load_snapshot()
        if existing is not None and not self._refresh_due(existing):
            return existing
        with self._condition:
            if self._refreshing:
                self._condition.wait_for(lambda: not self._refreshing)
                if self._last_error is not None:
                    raise self._last_error
                refreshed = self.database.load_snapshot()
                if refreshed is not None:
                    return refreshed
            self._refreshing = True
            self._last_error = None
        try:
            snapshot = self.provider.fetch_snapshot()
        except RateLimitError as error:
            snapshot = self._rate_limited_snapshot(error)
        except BaseException as error:
            with self._condition:
                self._last_error = error
            raise
        else:
            self.database.save_snapshot(snapshot)
        finally:
            with self._condition:
                self._refreshing = False
                self._condition.notify_all()
        return snapshot

    def _refresh_due(self, snapshot: ProjectSnapshot) -> bool:
        if snapshot.fetched_at is None:
            return True
        now = self._clock().astimezone(UTC)
        if snapshot.rate_limited_until is not None:
            try:
                if _parse_time(snapshot.rate_limited_until) > now:
                    return False
            except ValueError:
                return True
        try:
            fetched_at = _parse_time(snapshot.fetched_at)
        except ValueError:
            return True
        return (now - fetched_at).total_seconds() >= self.minimum_refresh_seconds

    def _rate_limited_snapshot(self, error: RateLimitError) -> ProjectSnapshot:
        existing = self.database.load_snapshot()
        if existing is None:
            existing = ProjectSnapshot(
                fetched_at=None,
                rate_limited_until=None,
                columns=(ProjectColumn(status="No status", items=()),),
            )
        snapshot = ProjectSnapshot(
            fetched_at=existing.fetched_at or self._clock().astimezone(UTC).isoformat(),
            rate_limited_until=error.rate_limited_until.astimezone(UTC).isoformat(),
            columns=existing.columns,
        )
        self.database.save_snapshot(snapshot)
        return snapshot


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
