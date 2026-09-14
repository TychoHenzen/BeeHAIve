from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, cast

from beehaiive.github.errors.provider_error import ProviderError as ProviderError
from beehaiive.github.graphql_protocol import GraphQLClient as GraphQLClient
from beehaiive.models import Stage, project_stage_from_status


def _required_text(value: object, label: str) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ProviderError("GitHub GraphQL returned an invalid object")
    return cast(Mapping[str, Any], value)


def require_graphql_mapping(value: object) -> Mapping[str, Any]:
    """Validate and narrow an object returned by the GitHub GraphQL API."""

    return _mapping(value)


def _nodes(value: object) -> list[Mapping[str, Any]]:
    container = _mapping(value)
    raw_nodes: object = container.get("nodes", [])
    if not isinstance(raw_nodes, list):
        raise ProviderError("GitHub GraphQL returned invalid nodes")
    node_values = cast(list[object], raw_nodes)
    return [_mapping(node) for node in node_values if node is not None]


def _project(data: Mapping[str, Any], owner_type: str = "user") -> Mapping[str, Any]:
    owner = _mapping(data.get(owner_type))
    return _mapping(owner.get("projectV2"))


def _owner_query(query: str, owner_type: str) -> str:
    if owner_type not in {"user", "organization"}:
        raise ProviderError("GitHub Project owner type must be user or organization")
    return query.replace("user(login:", f"{owner_type}(login:")


def _next_cursor(
    connection: Mapping[str, Any],
    seen_cursors: set[str] | None = None,
    *,
    strict: bool = False,
) -> tuple[bool, str | None]:
    if strict:
        nodes_value: object = connection.get("nodes")
        if not isinstance(nodes_value, list) or not all(
            isinstance(node, Mapping) for node in cast(list[object], nodes_value)
        ):
            raise ProviderError("GitHub GraphQL connection returned invalid nodes")
        page_info_value: object = connection.get("pageInfo")
        if not isinstance(page_info_value, Mapping) or not isinstance(
            cast(Mapping[str, Any], page_info_value).get("hasNextPage"), bool
        ):
            raise ProviderError("GitHub GraphQL connection omitted pagination state")
    page_info = _mapping(connection.get("pageInfo", {}))
    has_next = page_info.get("hasNextPage") is True
    cursor = page_info.get("endCursor")
    if has_next and (not isinstance(cursor, str) or not cursor):
        raise ProviderError("GitHub GraphQL page did not include an end cursor")
    if has_next and isinstance(cursor, str) and seen_cursors is not None:
        if cursor in seen_cursors:
            raise ProviderError("GitHub GraphQL pagination repeated a cursor")
        seen_cursors.add(cursor)
    return has_next, cursor if isinstance(cursor, str) else None


def _connection_at(data: Mapping[str, Any], path: tuple[str, ...]) -> Mapping[str, Any]:
    value: object = data
    for key in path:
        value = _mapping(value).get(key)
    return _mapping(value)


def _complete_connection(
    client: GraphQLClient,
    initial: object,
    query: str,
    variables: Mapping[str, object],
    response_path: tuple[str, ...],
    *,
    strict: bool = False,
) -> dict[str, Any]:
    def validated_connection(value: object) -> Mapping[str, Any]:
        connection = _mapping(value)
        if strict:
            _next_cursor(connection, strict=True)
        return connection

    connection = validated_connection(initial)
    nodes = list(_nodes(connection))
    seen_cursors: set[str] = set()
    has_next, cursor = _next_cursor(connection, seen_cursors)
    while has_next:
        assert cursor is not None
        page_data = client.execute(
            query,
            {**variables, "cursor": cursor},
        )
        page = validated_connection(_connection_at(page_data, response_path))
        nodes.extend(_nodes(page))
        has_next, cursor = _next_cursor(page, seen_cursors)
    completed = dict(connection)
    completed["nodes"] = nodes
    completed["pageInfo"] = {"hasNextPage": False, "endCursor": None}
    return completed


def complete_graphql_connection(
    client: GraphQLClient,
    initial: object,
    query: str,
    variables: Mapping[str, object],
    response_path: tuple[str, ...],
    *,
    strict: bool = False,
) -> dict[str, Any]:
    """Complete a GraphQL connection, optionally rejecting incomplete pages."""

    return _complete_connection(
        client, initial, query, variables, response_path, strict=strict
    )


def _stage_from_status(status: str | None) -> Stage | None:
    return project_stage_from_status(status)


def _project_item_status_values(item: Mapping[str, Any]) -> list[str]:
    values: list[str] = []
    for field_value in _nodes(item.get("fieldValues", {})):
        raw_field = field_value.get("field")
        if raw_field is None:
            continue
        field = _mapping(raw_field)
        value = field_value.get("name")
        if field.get("name") == "Status" and isinstance(value, str):
            values.append(value)
    return values


def _project_status_index(
    items: Iterable[Mapping[str, Any]],
) -> tuple[dict[tuple[str, int], str | None], set[tuple[str, int]]]:
    statuses: dict[tuple[str, int], str | None] = {}
    conflicts: set[tuple[str, int]] = set()
    for item in items:
        content_value = item.get("content")
        if content_value is None:
            continue
        content = _mapping(content_value)
        repository_value = content.get("repository")
        if repository_value is None:
            continue
        repository_name = _mapping(repository_value).get("nameWithOwner")
        number = content.get("number")
        if not isinstance(repository_name, str) or not isinstance(number, int):
            continue
        key = (repository_name.casefold(), number)
        values = set(_project_item_status_values(item))
        status = next(iter(values)) if len(values) == 1 else None
        if len(values) > 1 or (key in statuses and statuses[key] != status):
            conflicts.add(key)
        statuses[key] = status
    return statuses, conflicts


def _actor_name(value: object) -> str | None:
    if not isinstance(value, Mapping):
        return None
    actor = cast(Mapping[str, Any], value)
    for key in ("login", "name"):
        name = actor.get(key)
        if isinstance(name, str) and name:
            return name
    return None


def _label_names(value: object) -> list[str]:
    names: list[str] = []
    for label in _nodes(value):
        name = label.get("name")
        if isinstance(name, str) and name:
            names.append(name)
    return names


def _pull_request_head_sha(value: object) -> str | None:
    if not isinstance(value, Mapping):
        return None
    head_ref = cast(Mapping[str, Any], value)
    target_value = head_ref.get("target")
    if not isinstance(target_value, Mapping):
        return None
    target = cast(Mapping[str, Any], target_value)
    oid = target.get("oid")
    return oid if isinstance(oid, str) and oid.strip() else None


def _commit_oid(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _review_status(state: object) -> str:
    normalized = str(state or "").strip().upper()
    if normalized == "APPROVED":
        return "pass"
    if normalized == "CHANGES_REQUESTED":
        return "fail"
    return "pending"


__all__ = [
    "_required_text",
    "_mapping",
    "require_graphql_mapping",
    "_nodes",
    "_project",
    "_owner_query",
    "_next_cursor",
    "_connection_at",
    "_complete_connection",
    "complete_graphql_connection",
    "_stage_from_status",
    "_project_item_status_values",
    "_project_status_index",
    "_actor_name",
    "_label_names",
    "_pull_request_head_sha",
    "_commit_oid",
    "_review_status",
]
