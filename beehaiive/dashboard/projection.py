from __future__ import annotations

from collections.abc import Mapping, Sequence

from .values import mapping, mappings, sequence
from .views import pbi_view, repository_view


def build_dashboard_state(
    state: Mapping[str, object],
    actions: Sequence[Mapping[str, object]] = (),
    include_archived: bool = False,
) -> dict[str, object]:
    """Build the dashboard contract without changing orchestration state."""

    repositories: list[dict[str, object]] = []
    all_pbis: list[dict[str, object]] = []
    recent_deliveries: list[dict[str, object]] = []
    for raw_repository in mappings(state.get("repositories")):
        repository = repository_view(raw_repository, actions, include_archived)
        repositories.append(repository)
        all_pbis.extend(mappings(repository.get("pbis")))
        for raw_pbi in mappings(raw_repository.get("pbis")):
            pbi = pbi_view(raw_pbi, raw_repository.get("name"), actions)
            if (
                pbi.get("archived")
                or pbi.get("result")
                or sequence(pbi.get("pull_requests"))
            ):
                recent_deliveries.append(
                    {
                        "repository": raw_repository.get("name"),
                        "project": state.get("name"),
                        "pbi": pbi,
                    }
                )

    readers = sum(len(sequence(pbi.get("readers"))) for pbi in all_pbis)
    readers += sum(
        len(mapping(pbi.get("reviewers")))
        for pbi in all_pbis
        if not sequence(pbi.get("readers"))
    )
    subtasks = sum(len(sequence(pbi.get("subtasks"))) for pbi in all_pbis)
    active_runs = sum(1 for pbi in all_pbis if pbi.get("status") == "active")
    awaiting_operator_runs = sum(
        1 for pbi in all_pbis if pbi.get("status") == "awaiting_operator"
    )
    failed_runs = sum(1 for pbi in all_pbis if pbi.get("status") == "failed")
    completed_runs = sum(1 for pbi in all_pbis if pbi.get("status") == "completed")
    active_repositories = sum(
        1 for repository in repositories if repository.get("active") is True
    )
    active_writers = sum(
        1
        for repository in repositories
        if mapping(repository.get("writer")).get("status") == "active"
    )

    return {
        "project": {
            "id": state.get("project_id"),
            "name": state.get("name"),
            "updated_at": state.get("updated_at"),
        },
        "project_id": state.get("project_id"),
        "name": state.get("name"),
        "updated_at": state.get("updated_at"),
        "event_limit": state.get("event_limit"),
        "counts": {
            "projects": 1,
            "repositories": len(repositories),
            "active_repositories": active_repositories,
            "pbis": len(all_pbis),
            "subtasks": subtasks,
            "writers": active_writers,
            "readers": readers,
            "active_runs": active_runs,
            "awaiting_operator_runs": awaiting_operator_runs,
            "failed_runs": failed_runs,
            "completed_runs": completed_runs,
        },
        "repositories": repositories,
        "recent_deliveries": recent_deliveries[-100:],
        "actions": [dict(action) for action in actions],
    }
