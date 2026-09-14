from __future__ import annotations

from collections import deque
from typing import Any

from .constants import MAX_PBI_RELATION_GRAPH_ISSUES
from .graph_helpers import _contains_dependency_cycle, _failure_code
from .pbi_relation_dependency import PbiRelationDependency
from .pbi_relation_error import PbiRelationError
from .pbi_relation_provider_error import PbiRelationProviderError
from .pbi_relation_request import PbiRelationRequest
from .pbi_relation_result import PbiRelationResult
from .pbi_relation_validation_error import PbiRelationValidationError


class PbiRelationApplyMixin:
    def apply(self: Any, request: PbiRelationRequest) -> PbiRelationResult:
        request.validate()
        requested_children = tuple(child.number for child in request.children)
        requested_dependencies = request.dependencies
        empty_dependencies: tuple[PbiRelationDependency, ...] = ()
        try:
            snapshot = self._provider.prepare_pbi_relations(request)
            child_issues = {issue.number: issue for issue in snapshot.children}
            if set(child_issues) != set(requested_children):
                raise PbiRelationProviderError("child_readback_incomplete")
            if set(snapshot.parent_by_child) != set(requested_children):
                raise PbiRelationProviderError("child_parent_readback_incomplete")
            parent_children = {issue.number for issue in snapshot.parent_sub_issues}
            for child_number, existing_parent in snapshot.parent_by_child.items():
                if existing_parent not in (None, snapshot.parent.number):
                    raise PbiRelationValidationError(
                        "Child already belongs to another parent",
                        code="child_has_other_parent",
                        status_code=409,
                    )
                if (existing_parent == snapshot.parent.number) != (
                    child_number in parent_children
                ):
                    raise PbiRelationProviderError("sub_issue_readback_conflict")
            blockers_by_child = {
                number: self._provider.list_pbi_blocked_by(request.repository, number)
                for number in requested_children
            }
            graph = self._complete_blocking_graph(
                request.repository, requested_children
            )
            existing_dependencies = {
                (blocker.number, blocked)
                for blocked, blockers in blockers_by_child.items()
                for blocker in blockers
            }
            for blocked, blockers in blockers_by_child.items():
                for blocker in blockers:
                    graph.setdefault(blocker.number, set()).add(blocked)
            if _contains_dependency_cycle(graph):
                raise PbiRelationValidationError(
                    "The existing dependency graph contains a cycle",
                    code="existing_dependency_cycle",
                    status_code=409,
                )
            planned_graph = {
                node: set(dependents) for node, dependents in graph.items()
            }
            for edge in requested_dependencies:
                planned_graph.setdefault(edge.blocked_by_issue_number, set()).add(
                    edge.blocked_issue_number
                )
                planned_graph.setdefault(edge.blocked_issue_number, set())
            if _contains_dependency_cycle(planned_graph):
                raise PbiRelationValidationError(
                    "Dependency declarations would create a cycle",
                    code="dependency_cycle",
                    status_code=409,
                )
        except PbiRelationError:
            raise
        except Exception as exc:
            return self._incomplete(
                request,
                failure_code=_failure_code(exc),
                pending_step="preflight",
            )

        existing_sub_issue_numbers = {
            issue.number for issue in snapshot.parent_sub_issues
        }
        preexisting_children = tuple(
            number
            for number in requested_children
            if number in existing_sub_issue_numbers
        )
        preexisting_dependencies = tuple(
            edge
            for edge in requested_dependencies
            if (edge.blocked_by_issue_number, edge.blocked_issue_number)
            in existing_dependencies
        )
        completed_steps = ["preflight"]
        write_error: str | None = None
        for child in request.children:
            if child.number in preexisting_children:
                continue
            try:
                self._provider.add_pbi_sub_issue(
                    request.repository,
                    snapshot.parent.number,
                    child_issues[child.number].id,
                )
            except Exception as exc:
                write_error = _failure_code(exc)
                break
        if any(child.number not in preexisting_children for child in request.children):
            completed_steps.append("sub_issue_write")
        sub_issue_readback_complete = True
        observed_children: set[int]
        try:
            observed_children = {
                issue.number
                for issue in self._provider.list_pbi_sub_issues(
                    request.repository, snapshot.parent.number
                )
            }
        except Exception as exc:
            observed_children = set()
            sub_issue_readback_complete = False
            write_error = write_error or _failure_code(exc)
        observed_parents: dict[int, int | None] = {}
        for number in requested_children:
            try:
                observed_parents[number] = self._provider.get_pbi_parent_issue_number(
                    request.repository, number
                )
            except Exception as exc:
                sub_issue_readback_complete = False
                write_error = write_error or _failure_code(exc)
        confirmed_children = tuple(
            number
            for number in requested_children
            if number in observed_children
            and observed_parents.get(number) == snapshot.parent.number
        )
        if sub_issue_readback_complete:
            completed_steps.append("sub_issue_readback")

        if not sub_issue_readback_complete or len(confirmed_children) != len(
            requested_children
        ):
            try:
                confirmed_dependencies, dependency_readback_complete = (
                    self._read_dependency_confirmation(
                        request.repository, requested_dependencies
                    )
                )
            except Exception as exc:
                confirmed_dependencies = empty_dependencies
                dependency_readback_complete = False
                write_error = write_error or _failure_code(exc)
            pending_dependencies = tuple(
                edge
                for edge in requested_dependencies
                if edge not in confirmed_dependencies
            )
            return PbiRelationResult(
                "incomplete",
                snapshot.parent,
                request.parent_issue_number,
                requested_children,
                preexisting_children,
                confirmed_children,
                tuple(
                    number
                    for number in requested_children
                    if number not in confirmed_children
                ),
                requested_dependencies,
                preexisting_dependencies,
                confirmed_dependencies,
                pending_dependencies,
                sub_issue_readback_complete,
                dependency_readback_complete,
                tuple(completed_steps),
                "link_sub_issues",
                write_error or "relation_readback_incomplete",
            )

        write_error = None
        for edge in request.dependencies:
            if edge in preexisting_dependencies:
                continue
            try:
                self._provider.add_pbi_dependency(
                    request.repository,
                    edge.blocked_issue_number,
                    child_issues[edge.blocked_by_issue_number].id,
                )
            except Exception as exc:
                write_error = _failure_code(exc)
                break
        if any(edge not in preexisting_dependencies for edge in request.dependencies):
            completed_steps.append("dependency_write")
        try:
            confirmed_dependencies, dependency_readback_complete = (
                self._read_dependency_confirmation(
                    request.repository, requested_dependencies
                )
            )
            completed_steps.append("dependency_readback")
        except Exception as exc:
            confirmed_dependencies = empty_dependencies
            dependency_readback_complete = False
            write_error = write_error or _failure_code(exc)

        pending_children = tuple(
            number for number in requested_children if number not in confirmed_children
        )
        pending_dependencies = tuple(
            edge
            for edge in requested_dependencies
            if edge not in confirmed_dependencies
        )
        complete = not pending_children and not pending_dependencies
        return PbiRelationResult(
            "complete" if complete else "incomplete",
            snapshot.parent,
            request.parent_issue_number,
            requested_children,
            preexisting_children,
            confirmed_children,
            pending_children,
            requested_dependencies,
            preexisting_dependencies,
            confirmed_dependencies,
            pending_dependencies,
            sub_issue_readback_complete,
            dependency_readback_complete,
            tuple(completed_steps),
            None if complete else "record_dependencies",
            None if complete else write_error or "relation_readback_incomplete",
        )

    def _complete_blocking_graph(
        self: Any, repository: str, starting_issues: tuple[int, ...]
    ) -> dict[int, set[int]]:
        graph: dict[int, set[int]] = {}
        queue = deque(starting_issues)
        while queue:
            number = queue.popleft()
            if number in graph:
                continue
            if len(graph) >= MAX_PBI_RELATION_GRAPH_ISSUES:
                raise PbiRelationProviderError("dependency_graph_too_large")
            dependents = self._provider.list_pbi_blocking(repository, number)
            graph[number] = {issue.number for issue in dependents}
            queue.extend(
                dependent for dependent in graph[number] if dependent not in graph
            )
        return graph

    def _read_dependency_confirmation(
        self: Any, repository: str, dependencies: tuple[PbiRelationDependency, ...]
    ) -> tuple[tuple[PbiRelationDependency, ...], bool]:
        blocked_numbers = tuple(
            dict.fromkeys(edge.blocked_issue_number for edge in dependencies)
        )
        blockers = {
            number: {
                issue.number
                for issue in self._provider.list_pbi_blocked_by(repository, number)
            }
            for number in blocked_numbers
        }
        confirmed = tuple(
            edge
            for edge in dependencies
            if edge.blocked_by_issue_number
            in blockers.get(edge.blocked_issue_number, set())
        )
        return confirmed, True


__all__ = ["PbiRelationApplyMixin"]
