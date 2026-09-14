from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

__all__ = ["PbiRefinementQuestion"]


@dataclass(frozen=True, slots=True)
class PbiRefinementQuestion:
    """A bounded question and its answer revisions within one generation."""

    question_id: str
    text: str
    answer_history: tuple[Mapping[str, object], ...] = ()
    evidence_refs: tuple[str, ...] = ()

    @property
    def revision(self) -> int:
        return len(self.answer_history)

    def as_dict(self) -> dict[str, object]:
        return {
            "question_id": self.question_id,
            "text": self.text,
            "revision": self.revision,
            "answer": (
                self.answer_history[-1].get("text") if self.answer_history else None
            ),
            "answer_history": [dict(answer) for answer in self.answer_history],
            "evidence_refs": list(self.evidence_refs),
        }
