from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from .helpers.lease_helpers import _now as _now


class RuntimeSettingsMixin:
    def get_runtime_settings(self: Any) -> dict[str, object]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT key, value_json FROM runtime_settings ORDER BY key"
            ).fetchall()
        settings: dict[str, object] = {}
        for row in rows:
            try:
                settings[str(row["key"])] = json.loads(str(row["value_json"]))
            except json.JSONDecodeError:
                continue
        return settings

    def update_runtime_settings(
        self: Any, updates: Mapping[str, object]
    ) -> dict[str, object]:
        with self._transaction() as connection:
            for key, value in updates.items():
                if not key.strip():
                    raise ValueError("Runtime setting keys must be non-empty")
                encoded = json.dumps(value, sort_keys=True)
                connection.execute(
                    """
                    INSERT INTO runtime_settings(key, value_json, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value_json = excluded.value_json,
                        updated_at = excluded.updated_at
                    """,
                    (key, encoded, _now()),
                )
        return self.get_runtime_settings()


__all__ = ["RuntimeSettingsMixin"]
