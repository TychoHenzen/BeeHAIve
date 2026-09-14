from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

MAX_PBI_RELATION_CHILDREN = 20
MAX_PBI_RELATION_DEPENDENCIES = 100
MAX_PBI_RELATION_GRAPH_ISSUES = 500


class PbiRelationError(ValueError):
    def __init__(self, message: str, *, code: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class PbiRelationValidationError(PbiRelationError):
    pass


class PbiRelationScopeError(PbiRelationError):
    def __init__(
        self, message: str = "Project or repository is not authorized"
    ) -> None:
        super().__init__(message, code="scope_denied", status_code=403)


class PbiRelationProviderError(RuntimeError):
    def __init__(self, code: str = "github_request_failed") -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class PbiCreatedIssueReference:
    repository: str
    node_id: str
    number: int
    url: str
    project_item_id: str


@dataclass(frozen=True, slots=True)
class PbiRelationDependency:
    blocked_issue_number: int
    blocked_by_issue_number: int

    def as_dict(self) -> dict[str, int]:
        return {
            "blocked_issue_number": self.blocked_issue_number,
            "blocked_by_issue_number": self.blocked_by_issue_number,
        }


@dataclass(frozen=True, slots=True)
class PbiRelationRequest:
    project_id: str
    repository: str
    parent_issue_number: int
    children: tuple[PbiCreatedIssueReference, ...]
    dependencies: tuple[PbiRelationDependency, ...]

    def validate(self) -> None:
        if not self.project_id.strip():
            raise PbiRelationValidationError(
                "Project id is required", code="invalid_project"
            )
        owner, separator, name = self.repository.partition("/")
        if not separator or not owner or not name or "/" in name:
            raise PbiRelationValidationError(
                "Repository must use owner/name format", code="invalid_repository"
            )
        if self.parent_issue_number <= 0:
            raise PbiRelationValidationError(
                "Parent issue number must be positive", code="invalid_parent"
            )
        if not 1 <= len(self.children) <= MAX_PBI_RELATION_CHILDREN:
            raise PbiRelationValidationError(
                "Child count is outside the supported range", code="invalid_children"
            )
        child_numbers: set[int] = set()
        child_node_ids: set[str] = set()
        project_item_ids: set[str] = set()
        for child in self.children:
            if (
                child.number <= 0
                or not child.node_id.strip()
                or not child.url.strip()
                or not child.project_item_id.strip()
            ):
                raise PbiRelationValidationError(
                    "Created child reference is incomplete", code="invalid_child"
                )
            if child.repository.casefold() != self.repository.casefold():
                raise PbiRelationValidationError(
                    "Child repository does not match the request repository",
                    code="cross_repository_child",
                )
            if child.number == self.parent_issue_number:
                raise PbiRelationValidationError(
                    "Parent cannot be one of its own children", code="self_link"
                )
            if (
                child.number in child_numbers
                or child.node_id in child_node_ids
                or child.project_item_id in project_item_ids
            ):
                raise PbiRelationValidationError(
                    "Child references must be unique", code="duplicate_child"
                )
            child_numbers.add(child.number)
            child_node_ids.add(child.node_id)
            project_item_ids.add(child.project_item_id)
        if len(self.dependencies) > MAX_PBI_RELATION_DEPENDENCIES:
            raise PbiRelationValidationError(
                "Dependency count exceeds the supported limit",
                code="too_many_dependencies",
            )
        dependency_pairs: set[tuple[int, int]] = set()
        for dependency in self.dependencies:
            blocked = dependency.blocked_issue_number
            blocker = dependency.blocked_by_issue_number
            if blocked <= 0 or blocker <= 0:
                raise PbiRelationValidationError(
                    "Dependency issue numbers must be positive",
                    code="invalid_dependency",
                )
            if blocked == blocker:
                raise PbiRelationValidationError(
                    "An issue cannot block itself", code="self_dependency"
                )
            if blocked not in child_numbers or blocker not in child_numbers:
                raise PbiRelationValidationError(
                    "Dependencies must refer to declared children",
                    code="dependency_outside_children",
                )
            pair = (blocked, blocker)
            if pair in dependency_pairs:
                raise PbiRelationValidationError(
                    "Dependency declarations must be unique",
                    code="duplicate_dependency",
                )
            dependency_pairs.add(pair)


@dataclass(frozen=True, slots=True)
class PbiRelationIssue:
    id: int
    node_id: str
    number: int
    url: str
    title: str
    state: str

    def as_dict(self) -> dict[str, object]:
        return {
            "number": self.number,
            "url": self.url,
            "state": self.state,
        }


@dataclass(frozen=True, slots=True)
class PbiRelationSnapshot:
    parent: PbiRelationIssue
    children: tuple[PbiRelationIssue, ...]
    parent_sub_issues: tuple[PbiRelationIssue, ...]
    parent_by_child: Mapping[int, int | None]


class PbiRelationProvider(Protocol):
    def prepare_pbi_relations(
        self, request: PbiRelationRequest
    ) -> PbiRelationSnapshot: ...

    def list_pbi_sub_issues(
        self, repository: str, parent_issue_number: int
    ) -> tuple[PbiRelationIssue, ...]: ...

    def list_pbi_blocked_by(
        self, repository: str, issue_number: int
    ) -> tuple[PbiRelationIssue, ...]: ...

    def list_pbi_blocking(
        self, repository: str, issue_number: int
    ) -> tuple[PbiRelationIssue, ...]: ...

    def add_pbi_sub_issue(
        self, repository: str, parent_issue_number: int, child_issue_id: int
    ) -> None: ...

    def add_pbi_dependency(
        self, repository: str, blocked_issue_number: int, blocker_issue_id: int
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class PbiRelationResult:
    status: Literal["complete", "incomplete"]
    parent: PbiRelationIssue | None
    parent_issue_number: int
    requested_children: tuple[int, ...]
    preexisting_children: tuple[int, ...]
    confirmed_children: tuple[int, ...]
    pending_children: tuple[int, ...]
    requested_dependencies: tuple[PbiRelationDependency, ...]
    preexisting_dependencies: tuple[PbiRelationDependency, ...]
    confirmed_dependencies: tuple[PbiRelationDependency, ...]
    pending_dependencies: tuple[PbiRelationDependency, ...]
    sub_issue_readback_complete: bool
    dependency_readback_complete: bool
    completed_steps: tuple[str, ...]
    pending_step: str | None = None
    failure_code: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "parent": (
                self.parent.as_dict()
                if self.parent is not None
                else {"number": self.parent_issue_number}
            ),
            "relations": {
                "sub_issues": {
                    "requested": list(self.requested_children),
                    "preexisting": list(self.preexisting_children),
                    "confirmed": [
                        {
                            "parent_issue_number": self.parent_issue_number,
                            "child_issue_number": number,
                        }
                        for number in self.confirmed_children
                    ],
                    "pending": list(self.pending_children),
                    "readback_complete": self.sub_issue_readback_complete,
                },
                "dependencies": {
                    "requested": [
                        edge.as_dict() for edge in self.requested_dependencies
                    ],
                    "preexisting": [
                        edge.as_dict() for edge in self.preexisting_dependencies
                    ],
                    "confirmed": [
                        edge.as_dict() for edge in self.confirmed_dependencies
                    ],
                    "pending": [edge.as_dict() for edge in self.pending_dependencies],
                    "readback_complete": self.dependency_readback_complete,
                },
            },
            "completed_steps": list(self.completed_steps),
            "pending_step": self.pending_step,
            "failure_code": self.failure_code,
        }


class PbiRelationService:
    def __init__(self, provider: PbiRelationProvider) -> None:
        self._provider = provider

    def apply(self, request: PbiRelationRequest) -> PbiRelationResult:
        request.validate()
        requested_children = tuple(child.number for child in request.children)
        requested_dependencies = request.dependencies
        empty_children: tuple[int, ...] = ()
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
        try:
            observed_children = self._provider.list_pbi_sub_issues(
                request.repository, snapshot.parent.number
            )
            confirmed_children = tuple(
                number
                for number in requested_children
                if number in {issue.number for issue in observed_children}
            )
            sub_issue_readback_complete = True
            completed_steps.append("sub_issue_readback")
        except Exception as exc:
            confirmed_children = empty_children
            sub_issue_readback_complete = False
            write_error = write_error or _failure_code(exc)

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
        self, repository: str, starting_issues: tuple[int, ...]
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
        self, repository: str, dependencies: tuple[PbiRelationDependency, ...]
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

    @staticmethod
    def _incomplete(
        request: PbiRelationRequest, *, failure_code: str, pending_step: str
    ) -> PbiRelationResult:
        children = tuple(child.number for child in request.children)
        return PbiRelationResult(
            "incomplete",
            None,
            request.parent_issue_number,
            children,
            (),
            (),
            children,
            request.dependencies,
            (),
            (),
            request.dependencies,
            False,
            False,
            (),
            pending_step,
            failure_code,
        )


def _contains_dependency_cycle(graph: Mapping[int, set[int]]) -> bool:
    nodes = set(graph)
    for dependents in graph.values():
        nodes.update(dependents)
    indegree = dict.fromkeys(nodes, 0)
    for dependents in graph.values():
        for dependent in dependents:
            indegree[dependent] += 1
    queue = deque(number for number, degree in indegree.items() if degree == 0)
    visited = 0
    while queue:
        number = queue.popleft()
        visited += 1
        for dependent in graph.get(number, set()):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                queue.append(dependent)
    return visited != len(nodes)


def _failure_code(error: Exception) -> str:
    code = getattr(error, "code", None)
    return code if isinstance(code, str) else "provider_error"
