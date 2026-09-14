from __future__ import annotations

from collections import deque
from collections.abc import Mapping


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


__all__ = ["_contains_dependency_cycle", "_failure_code"]
