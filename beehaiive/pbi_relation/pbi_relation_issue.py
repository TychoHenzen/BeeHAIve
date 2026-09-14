from __future__ import annotations

from dataclasses import dataclass

__all__ = ["PbiRelationIssue"]


@dataclass(frozen=True, slots=True)
class PbiRelationIssue:
    id: int
    node_id: str
    number: int
    url: str
    title: str
    state: str
    state_reason: str | None = None

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "number": self.number,
            "url": self.url,
            "state": self.state,
        }
        if self.state_reason is not None:
            result["state_reason"] = self.state_reason
        return result
