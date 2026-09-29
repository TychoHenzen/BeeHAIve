from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, cast
from urllib.parse import quote

from .config import CoreConfig
from .models import ProjectCard, ProjectColumn, ProjectSnapshot
from .rest import GithubRestClient, next_link


class ProjectDataError(RuntimeError):
    pass


class ProjectProvider:
    def __init__(
        self,
        config: CoreConfig,
        client: GithubRestClient,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self.client = client
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def project_url(self) -> str:
        owner = quote(self.config.owner, safe="")
        prefix = "users" if self.config.owner_type == "user" else "orgs"
        return (
            f"https://api.github.com/{prefix}/{owner}/projectsV2/"
            f"{self.config.project_number}"
        )

    def fetch_snapshot(self) -> ProjectSnapshot:
        self.client.get_json(self.project_url)
        fields_payload = self.client.get_json(f"{self.project_url}/fields")
        status_field = _status_field(_list_payload(fields_payload.value, "fields"))
        options = _status_options(status_field)
        option_ids = _status_option_ids(status_field)
        items_url = f"{self.project_url}/items"
        items_payload = self.client.get_json(
            items_url,
            params={"per_page": 100, "fields": str(status_field["id"])},
        )
        raw_items = _list_payload(items_payload.value, "items")
        all_items = [_object(item) for item in raw_items if isinstance(item, dict)]
        while True:
            following = next_link(items_payload.headers)
            if following is None:
                break
            items_payload = self.client.get_json(following)
            all_items.extend(
                _object(item)
                for item in _list_payload(items_payload.value, "items")
                if isinstance(item, dict)
            )
        columns: dict[str, list[ProjectCard]] = {name: [] for name in options}
        columns["No status"] = []
        for raw_item in all_items:
            card = _normalise_item(raw_item)
            if card is None:
                continue
            status = _item_status(
                raw_item, str(status_field["id"]), options, option_ids
            )
            columns.setdefault(status, columns["No status"]).append(card)
        return ProjectSnapshot(
            fetched_at=self._clock().astimezone(UTC).isoformat(),
            rate_limited_until=None,
            columns=tuple(
                ProjectColumn(status=name, items=tuple(columns[name]))
                for name in (*options, "No status")
            ),
            status_field_id=str(status_field["id"]),
        )

    def fetch_item(
        self,
        item_key: str,
        *,
        status_field_id: str | None,
        status_options: tuple[str, ...] = (),
    ) -> tuple[ProjectCard, str]:
        option_ids: dict[str, str] = {}
        options = status_options
        if status_field_id is None:
            fields_payload = self.client.get_json(f"{self.project_url}/fields")
            status_field = _status_field(_list_payload(fields_payload.value, "fields"))
            status_field_id = str(status_field["id"])
            options = _status_options(status_field)
            option_ids = _status_option_ids(status_field)
        payload = self.client.get_json(
            f"{self.project_url}/items/{quote(item_key, safe='')}",
            params={"fields": status_field_id},
        )
        raw_item = _object(payload.value)
        if isinstance(raw_item.get("item"), dict):
            raw_item = _object(raw_item["item"])
        card = _normalise_item(raw_item)
        if card is None:
            raise ProjectDataError(f"GitHub Project item {item_key!r} is unavailable")
        return card, _item_status(raw_item, status_field_id, options, option_ids)


def _list_payload(value: Any, key: str) -> list[Any]:
    if isinstance(value, list):
        return cast(list[Any], value)
    payload = _object(value)
    if isinstance(payload.get(key), list):
        return cast(list[Any], payload[key])
    raise ProjectDataError(f"GitHub Project response did not contain a {key} list")


def _status_field(fields: list[Any]) -> dict[str, Any]:
    matches = [
        _object(field)
        for field in fields
        if isinstance(field, dict)
        and _text(_object(field).get("name")).casefold() == "status"
    ]
    if len(matches) != 1:
        raise ProjectDataError(
            f"Expected exactly one Project Status field, found {len(matches)}"
        )
    field = matches[0]
    data_type = _text(field.get("data_type") or field.get("dataType")).casefold()
    options = field.get("options")
    if data_type not in {"", "single_select", "singleselect", "single-select"}:
        raise ProjectDataError("Project Status field is not single-select")
    if not isinstance(options, list):
        raise ProjectDataError("Project Status field has no options")
    if "id" not in field:
        raise ProjectDataError("Project Status field has no id")
    return field


def _status_options(field: Mapping[str, Any]) -> tuple[str, ...]:
    raw_options = _list(field.get("options", []))
    names: list[str] = []
    for option in raw_options:
        option_object = _object(option)
        name = _display_name(option_object.get("name"))
        if name:
            names.append(name)
    return tuple(names)


def _status_option_ids(field: Mapping[str, Any]) -> dict[str, str]:
    option_ids: dict[str, str] = {}
    for option in _list(field.get("options", [])):
        option_object = _object(option)
        name = _display_name(option_object.get("name"))
        option_id = option_object.get("id")
        if name and option_id is not None:
            option_ids[str(option_id)] = name
    return option_ids


def _normalise_item(item: Mapping[str, Any]) -> ProjectCard | None:
    raw_type = _text(item.get("content_type") or item.get("type"))
    type_name = {
        "issue": "Issue",
        "pullrequest": "PullRequest",
        "pull_request": "PullRequest",
        "draftissue": "DraftIssue",
        "draft_issue": "DraftIssue",
    }.get(raw_type.casefold().replace(" ", ""))
    if type_name is None:
        return None
    raw_content = item.get("content")
    content = _object(raw_content) if isinstance(raw_content, dict) else dict(item)
    repository = _repository_name(content) or _repository_name(item)
    number = _optional_int(content.get("number"))
    title = _text(content.get("title"))
    body = _text(content.get("body"))
    labels = _labels(content.get("labels"))
    linked = parse_closing_issue_numbers(body) if type_name == "PullRequest" else ()
    item_key = _optional_text(
        item.get("id")
        or item.get("item_id")
        or content.get("id")
        or content.get("node_id")
    )
    if item_key is None:
        item_key = ":".join(
            value
            for value in (
                type_name,
                repository or "",
                str(number) if number is not None else "",
                _optional_text(content.get("html_url") or content.get("url")) or "",
            )
            if value
        )
    return ProjectCard(
        type=type_name,
        repository=None if type_name == "DraftIssue" else repository,
        number=None if type_name == "DraftIssue" else number,
        title=title,
        url=_optional_text(content.get("html_url") or content.get("url")),
        state=_optional_text(content.get("state")),
        labels=labels,
        linked_issue_numbers=linked,
        item_key=item_key,
    )


def _item_status(
    item: Mapping[str, Any],
    field_id: str,
    options: tuple[str, ...],
    option_ids: Mapping[str, str],
) -> str:
    raw_fields = _list(item.get("fields") or item.get("field_values") or [])
    for field_value in raw_fields:
        field = _object(field_value)
        candidate_id = field.get("id") or field.get("field_id")
        name = _text(field.get("name")).casefold()
        if (
            candidate_id is not None
            and str(candidate_id) != field_id
            and name != "status"
        ):
            continue
        value = field.get("value", field)
        option_name = (
            _display_name(_object(value).get("name"))
            if isinstance(value, dict)
            else _text(value)
        )
        if option_name in options:
            return option_name
        option_id = _object(value).get("id") if isinstance(value, dict) else value
        if option_id is not None and str(option_id) in option_ids:
            return option_ids[str(option_id)]
    raw_status = item.get("status") or item.get("status_name")
    status = _display_name(raw_status)
    return status if status in options else "No status"


def _repository_name(content: Mapping[str, Any]) -> str | None:
    value = content.get("repository") or content.get("repo")
    if isinstance(value, dict):
        repository = _object(value)
        for key in ("full_name", "name_with_owner", "fullName"):
            if repository.get(key):
                return str(repository[key])
    if isinstance(value, str) and value:
        return value
    repository_url = content.get("repository_url")
    if isinstance(repository_url, str):
        parts = repository_url.rstrip("/").split("/")
        if len(parts) >= 2:
            return "/".join(parts[-2:])
    return None


def _labels(value: Any) -> tuple[str, ...]:
    labels: list[str] = []
    for label in _list(value):
        if isinstance(label, dict) and _object(label).get("name") is not None:
            labels.append(str(_object(label)["name"]))
        elif isinstance(label, str):
            labels.append(label)
    return tuple(labels)


def _display_name(value: Any) -> str:
    if isinstance(value, dict):
        object_value = _object(value)
        for key in ("raw", "html", "name"):
            candidate = object_value.get(key)
            if candidate is not None and candidate != "":
                return _display_name(candidate)
        return ""
    return _text(value)


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _optional_text(value: Any) -> str | None:
    text = _text(value)
    return text or None


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _object(value: Any) -> dict[str, Any]:
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return cast(list[Any], value) if isinstance(value, list) else []


_CLOSING_KEYWORDS = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?[ \t]+"
    r"(?:(?:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+))?#(\d+)\b",
    re.IGNORECASE,
)


def parse_closing_issue_numbers(body: str) -> tuple[int, ...]:
    numbers: list[int] = []
    for match in _CLOSING_KEYWORDS.finditer(body):
        number = int(match.group(1))
        if number not in numbers:
            numbers.append(number)
    return tuple(numbers)
