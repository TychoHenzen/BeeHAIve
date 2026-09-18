from __future__ import annotations

import json
from collections.abc import Collection, Mapping
from datetime import UTC, datetime
from typing import Any, cast

from beehaiive.contract_types.validation import _redact_text

from .constants import (
    DEFAULT_WORKER_HOST_HEARTBEAT_SECONDS,
    DEFAULT_WORKER_HOST_STALE_SECONDS,
    MAX_WORKER_HOST_CAPABILITIES,
    MAX_WORKER_HOST_CAPABILITY_LENGTH,
    MAX_WORKER_HOST_ID_LENGTH,
    MAX_WORKER_HOST_SLOTS,
    MAX_WORKER_HOST_STATUS_REASON_LENGTH,
)
from .errors import StoreError
from .helpers.lease_helpers import _now


class WorkerHostsMixin:
    def register_worker_host(
        self: Any,
        host_id: str,
        worker_slots: int,
        capabilities: Collection[str],
        status_reason: str = "",
        *,
        heartbeat_at: str | None = None,
    ) -> dict[str, object]:
        normalized_id = _host_id(host_id)
        normalized_slots = _worker_slots(worker_slots)
        normalized_capabilities = _capabilities(capabilities)
        normalized_reason = _status_reason(status_reason)
        timestamp = _timestamp(heartbeat_at) if heartbeat_at is not None else _now()
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO worker_hosts(
                    host_id, worker_slots, capabilities_json, registered_at,
                    heartbeat_at, status_reason
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(host_id) DO UPDATE SET
                    worker_slots = excluded.worker_slots,
                    capabilities_json = excluded.capabilities_json,
                    heartbeat_at = excluded.heartbeat_at,
                    status_reason = excluded.status_reason
                """,
                (
                    normalized_id,
                    normalized_slots,
                    json.dumps(normalized_capabilities),
                    timestamp,
                    timestamp,
                    normalized_reason,
                ),
            )
            row = connection.execute(
                "SELECT * FROM worker_hosts WHERE host_id = ?", (normalized_id,)
            ).fetchone()
            if row is None:
                raise StoreError("Worker host registration could not be read back")
            return self._worker_host_from_row(
                row,
                datetime.now(UTC),
                DEFAULT_WORKER_HOST_STALE_SECONDS,
            )

    def heartbeat_worker_host(
        self: Any,
        host_id: str,
        status_reason: str = "",
        *,
        heartbeat_at: str | None = None,
    ) -> dict[str, object]:
        normalized_id = _host_id(host_id)
        normalized_reason = _status_reason(status_reason)
        timestamp = _timestamp(heartbeat_at) if heartbeat_at is not None else _now()
        with self._transaction() as connection:
            updated = connection.execute(
                """
                UPDATE worker_hosts
                SET heartbeat_at = ?, status_reason = ?
                WHERE host_id = ?
                """,
                (timestamp, normalized_reason, normalized_id),
            )
            if updated.rowcount != 1:
                raise StoreError(f"Unknown worker host: {normalized_id}")
            row = connection.execute(
                "SELECT * FROM worker_hosts WHERE host_id = ?", (normalized_id,)
            ).fetchone()
            if row is None:
                raise StoreError("Worker host heartbeat could not be read back")
            return self._worker_host_from_row(
                row,
                datetime.now(UTC),
                DEFAULT_WORKER_HOST_STALE_SECONDS,
            )

    def worker_host_for_id(
        self: Any,
        host_id: str,
        *,
        now: str | None = None,
        heartbeat_seconds: float = DEFAULT_WORKER_HOST_HEARTBEAT_SECONDS,
        stale_seconds: float = DEFAULT_WORKER_HOST_STALE_SECONDS,
    ) -> dict[str, object]:
        normalized_id = _host_id(host_id)
        current, interval, stale = _liveness_window(
            now, heartbeat_seconds, stale_seconds
        )
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM worker_hosts WHERE host_id = ?", (normalized_id,)
            ).fetchone()
        if row is None:
            return _unknown_host(normalized_id)
        return self._worker_host_from_row(row, current, stale, interval)

    def worker_host_records(
        self: Any,
        *,
        now: str | None = None,
        heartbeat_seconds: float = DEFAULT_WORKER_HOST_HEARTBEAT_SECONDS,
        stale_seconds: float = DEFAULT_WORKER_HOST_STALE_SECONDS,
    ) -> tuple[dict[str, object], ...]:
        current, interval, stale = _liveness_window(
            now, heartbeat_seconds, stale_seconds
        )
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM worker_hosts ORDER BY host_id"
            ).fetchall()
        return tuple(
            self._worker_host_from_row(row, current, stale, interval) for row in rows
        )

    @staticmethod
    def _worker_host_from_row(
        row: Mapping[str, object],
        current: datetime,
        stale_seconds: float,
        heartbeat_seconds: float = DEFAULT_WORKER_HOST_HEARTBEAT_SECONDS,
    ) -> dict[str, object]:
        heartbeat = _parse_timestamp(str(row["heartbeat_at"]))
        liveness = "unknown"
        if heartbeat is not None:
            age = (current - heartbeat).total_seconds()
            if age >= 0 and age <= stale_seconds:
                liveness = "active"
            elif age > stale_seconds:
                liveness = "stale"
        try:
            capabilities = json.loads(str(row["capabilities_json"]))
        except (TypeError, ValueError):
            capabilities = []
        if not isinstance(capabilities, list):
            capabilities = []
        bounded_capabilities = [
            value
            for value in cast(list[object], capabilities[:MAX_WORKER_HOST_CAPABILITIES])
            if isinstance(value, str)
        ]
        worker_slots = row["worker_slots"]
        advertised_slots = worker_slots if isinstance(worker_slots, int) else 0
        return {
            "host_id": row["host_id"],
            "worker_slots": advertised_slots,
            "capabilities": bounded_capabilities,
            "capability_count": len(bounded_capabilities),
            "registered_at": row["registered_at"],
            "heartbeat_at": row["heartbeat_at"],
            "status_reason": row["status_reason"],
            "heartbeat_interval_seconds": heartbeat_seconds,
            "stale_after_seconds": stale_seconds,
            "liveness": liveness,
            "available_worker_slots": (advertised_slots if liveness == "active" else 0),
        }


def _host_id(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StoreError("A stable worker host id is required")
    if len(value.strip()) > MAX_WORKER_HOST_ID_LENGTH:
        raise StoreError("Worker host id is too long")
    normalized = _redact_text(value.strip(), MAX_WORKER_HOST_ID_LENGTH)
    if not normalized:
        raise StoreError("A stable worker host id is required")
    return normalized


def _worker_slots(value: object) -> int:
    if type(value) is not int or not 1 <= value <= MAX_WORKER_HOST_SLOTS:
        raise StoreError(f"Worker slots must be between 1 and {MAX_WORKER_HOST_SLOTS}")
    return value


def _capabilities(values: Collection[object]) -> list[str]:
    if isinstance(values, (str, bytes, bytearray)):
        raise StoreError("Worker capabilities must be a collection of strings")
    raw = list(values)
    if len(raw) > MAX_WORKER_HOST_CAPABILITIES:
        raise StoreError("Too many worker capabilities")
    normalized: set[str] = set()
    for value in raw:
        if not isinstance(value, str) or not value.strip():
            raise StoreError("Worker capabilities must not be blank")
        if len(value.strip()) > MAX_WORKER_HOST_CAPABILITY_LENGTH:
            raise StoreError("Worker capability is too long")
        normalized.add(_redact_text(value.strip(), MAX_WORKER_HOST_CAPABILITY_LENGTH))
    if "" in normalized:
        raise StoreError("Worker capabilities must not be blank")
    return sorted(normalized)


def _status_reason(value: object) -> str:
    if not isinstance(value, str):
        raise StoreError("Worker host status reason must be text")
    if len(value) > MAX_WORKER_HOST_STATUS_REASON_LENGTH:
        raise StoreError("Worker host status reason is too long")
    return _redact_text(value, MAX_WORKER_HOST_STATUS_REASON_LENGTH)


def _timestamp(value: str) -> str:
    parsed = _parse_timestamp(value)
    if parsed is None:
        raise StoreError("Worker host timestamps must include a timezone")
    return parsed.isoformat()


def _parse_timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _liveness_window(
    now: str | None, heartbeat_seconds: object, stale_seconds: object
) -> tuple[datetime, float, float]:
    if (
        not isinstance(heartbeat_seconds, (int, float))
        or heartbeat_seconds <= 0
        or not isinstance(stale_seconds, (int, float))
        or stale_seconds <= heartbeat_seconds
    ):
        raise StoreError("Worker stale threshold must exceed heartbeat interval")
    current = datetime.now(UTC) if now is None else _parse_timestamp(now)
    if current is None:
        raise StoreError("Worker host timestamps must include a timezone")
    return current, float(heartbeat_seconds), float(stale_seconds)


def _unknown_host(host_id: str) -> dict[str, object]:
    return {
        "host_id": host_id,
        "worker_slots": 0,
        "capabilities": [],
        "capability_count": 0,
        "registered_at": None,
        "heartbeat_at": None,
        "status_reason": "No registration record",
        "heartbeat_interval_seconds": DEFAULT_WORKER_HOST_HEARTBEAT_SECONDS,
        "stale_after_seconds": DEFAULT_WORKER_HOST_STALE_SECONDS,
        "liveness": "unknown",
        "available_worker_slots": 0,
    }


__all__ = ["WorkerHostsMixin"]
