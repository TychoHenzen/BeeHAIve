from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast


@dataclass(frozen=True)
class ProjectCard:
    type: str
    repository: str | None
    number: int | None
    title: str
    url: str | None
    state: str | None
    labels: tuple[str, ...]
    linked_issue_numbers: tuple[int, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "type": self.type,
            "repository": self.repository,
            "number": self.number,
            "title": self.title,
            "url": self.url,
            "state": self.state,
            "labels": list(self.labels),
            "linked_issue_numbers": list(self.linked_issue_numbers),
        }


@dataclass(frozen=True)
class ProjectColumn:
    status: str
    items: tuple[ProjectCard, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "items": [item.as_dict() for item in self.items],
        }


@dataclass(frozen=True)
class ProjectSnapshot:
    fetched_at: str | None
    rate_limited_until: str | None
    columns: tuple[ProjectColumn, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "fetched_at": self.fetched_at,
            "rate_limited_until": self.rate_limited_until,
            "columns": [column.as_dict() for column in self.columns],
        }

    @classmethod
    def from_dict(cls, value: Any) -> ProjectSnapshot:
        payload = _object(value)
        raw_columns = _list(payload.get("columns", []))
        columns: list[ProjectColumn] = []
        for raw_column_value in raw_columns:
            raw_column = _object(raw_column_value)
            items: list[ProjectCard] = []
            for raw_item_value in _list(raw_column.get("items", [])):
                raw_item = _object(raw_item_value)
                labels = _list(raw_item.get("labels", []))
                linked = _list(raw_item.get("linked_issue_numbers", []))
                items.append(
                    ProjectCard(
                        type=str(raw_item.get("type", "")),
                        repository=_optional_string(raw_item.get("repository")),
                        number=_optional_int(raw_item.get("number")),
                        title=str(raw_item.get("title", "")),
                        url=_optional_string(raw_item.get("url")),
                        state=_optional_string(raw_item.get("state")),
                        labels=tuple(
                            str(label)
                            for label in labels
                            if isinstance(label, (str, int, float))
                        ),
                        linked_issue_numbers=tuple(
                            int(number) for number in linked if _is_int(number)
                        ),
                    )
                )
            columns.append(
                ProjectColumn(
                    status=str(raw_column.get("status", "No status")),
                    items=tuple(items),
                )
            )
        return cls(
            fetched_at=_optional_string(payload.get("fetched_at")),
            rate_limited_until=_optional_string(payload.get("rate_limited_until")),
            columns=tuple(columns),
        )


def _object(value: Any) -> dict[str, Any]:
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return cast(list[Any], value) if isinstance(value, list) else []


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _optional_int(value: Any) -> int | None:
    if not _is_int(value):
        return None
    return int(value)


def _is_int(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, str) and value.isdigit()
