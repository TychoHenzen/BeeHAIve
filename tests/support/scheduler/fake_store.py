from __future__ import annotations


class FakeStore:
    def __init__(self, states: dict[str, dict[str, object]]) -> None:
        self.states = states

    def project_state(self, project_id: str) -> dict[str, object]:
        return self.states[project_id]
