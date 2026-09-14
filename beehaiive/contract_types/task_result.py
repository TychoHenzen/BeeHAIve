from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import cast

from .constants import MAX_CONTRACT_TEXT
from .contract_error import ContractError
from .task_contract import TaskContract
from .task_outcome import TaskOutcome
from .validation import _bounded_value, _redact_text, _text

__all__ = ["TaskResult"]


@dataclass(frozen=True, slots=True)
class TaskResult:
    """Validated terminal result emitted by one worker step."""

    outcome: TaskOutcome
    evidence: Mapping[str, object]
    artifact_refs: tuple[Mapping[str, object], ...] = ()
    question: str | None = None
    required_action: str | None = None
    validation_reason: str | None = None
    answer: str | None = None

    @classmethod
    def invalid(cls, reason: str) -> TaskResult:
        return cls(
            TaskOutcome.FAIL,
            {},
            validation_reason=_redact_text(reason, MAX_CONTRACT_TEXT)
            or "Invalid task result",
        )

    @classmethod
    def from_payload(
        cls, value: object, contract: TaskContract, *, allow_answer: bool = False
    ) -> TaskResult:
        if not isinstance(value, Mapping):
            raise ContractError("Task result must be an object")
        mapping = cast(Mapping[str, object], value)
        allowed_keys = {
            "outcome",
            "evidence",
            "artifact_refs",
            "question",
            "required_action",
            "validation_reason",
            "answer",
        }
        unknown = set(mapping) - allowed_keys
        if unknown:
            raise ContractError(
                f"Unknown task result fields: {', '.join(map(str, unknown))}"
            )
        raw_outcome = mapping.get("outcome")
        if not isinstance(raw_outcome, str):
            raise ContractError("Task outcome must be a string")
        try:
            outcome = TaskOutcome(raw_outcome)
        except ValueError as exc:
            raise ContractError(f"Unknown task outcome: {raw_outcome}") from exc
        missing_fields = [
            field for field in ("evidence", "artifact_refs") if field not in mapping
        ]
        if missing_fields:
            raise ContractError(
                "Task result is missing required fields: " + ", ".join(missing_fields)
            )
        evidence = mapping["evidence"]
        if not isinstance(evidence, Mapping):
            raise ContractError("Task result evidence must be an object")
        evidence = cast(Mapping[str, object], evidence)
        raw_artifacts = mapping["artifact_refs"]
        if not isinstance(raw_artifacts, Sequence) or isinstance(
            raw_artifacts, (str, bytes, bytearray)
        ):
            raise ContractError("Task result artifact_refs must be a list")
        raw_artifacts = cast(Sequence[object], raw_artifacts)
        artifact_refs: list[Mapping[str, object]] = []
        for artifact in raw_artifacts:
            if not isinstance(artifact, Mapping):
                raise ContractError("Task artifact references must be objects")
            bounded = _bounded_value(cast(Mapping[str, object], artifact))
            if not isinstance(bounded, dict):  # pragma: no cover - mapping input
                raise ContractError("Task artifact references must be objects")
            artifact_refs.append(cast(Mapping[str, object], bounded))
        result = cls(
            outcome,
            cast(Mapping[str, object], _bounded_value(evidence)),
            tuple(artifact_refs),
            _text(mapping.get("question"), "Question", required=False),
            _text(mapping.get("required_action"), "Required action", required=False),
            _text(
                mapping.get("validation_reason"),
                "Validation reason",
                required=False,
            ),
            _text(mapping.get("answer"), "Answer", required=False),
        )
        if result.answer is not None and not allow_answer:
            raise ContractError("Worker task results cannot contain an answer")
        return result.validated(contract)

    def validated(self, contract: TaskContract) -> TaskResult:
        contract.validate()
        if self.outcome not in contract.allowed_outcomes:
            raise ContractError(f"Outcome is not allowed: {self.outcome.value}")
        question = _text(self.question, "Question", required=False)
        required_action = _text(self.required_action, "Required action", required=False)
        validation_reason = _text(
            self.validation_reason, "Validation reason", required=False
        )
        answer = _text(self.answer, "Answer", required=False)
        bounded_evidence = _bounded_value(self.evidence)
        if not isinstance(bounded_evidence, dict):  # pragma: no cover - mapping input
            raise ContractError("Task result evidence must be an object")
        bounded_evidence = cast(dict[str, object], bounded_evidence)
        if self.outcome is TaskOutcome.PASS:
            missing_evidence = [
                key for key in contract.required_evidence if key not in bounded_evidence
            ]
            if missing_evidence:
                raise ContractError(
                    "Missing required evidence: " + ", ".join(missing_evidence)
                )
            bounded_inputs = cast(dict[str, object], _bounded_value(contract.inputs))
            mismatched_evidence = [
                key
                for key in contract.required_evidence
                if key in bounded_inputs
                and json.dumps(bounded_evidence[key], sort_keys=True)
                != json.dumps(bounded_inputs[key], sort_keys=True)
            ]
            if mismatched_evidence:
                raise ContractError(
                    "Evidence does not match contract inputs: "
                    + ", ".join(mismatched_evidence)
                )
        declared = {artifact.artifact_id for artifact in contract.required_artifacts}
        references: list[Mapping[str, object]] = []
        for reference in self.artifact_refs:
            bounded = _bounded_value(reference)
            if not isinstance(bounded, dict):  # pragma: no cover - mapping input
                raise ContractError("Task artifact references must be objects")
            bounded = cast(dict[str, object], bounded)
            artifact_id = bounded.get("id")
            if not isinstance(artifact_id, str) or artifact_id not in declared:
                raise ContractError("Task result references an undeclared artifact")
            references.append(bounded)
        if self.outcome is TaskOutcome.PASS:
            present = {str(reference["id"]) for reference in references}
            missing_artifacts = [
                artifact.artifact_id
                for artifact in contract.required_artifacts
                if artifact.required and artifact.artifact_id not in present
            ]
            if missing_artifacts:
                raise ContractError(
                    "Missing required artifacts: " + ", ".join(missing_artifacts)
                )
        if self.outcome is TaskOutcome.BLOCKED and not required_action:
            raise ContractError("Blocked results require a required_action")
        if self.outcome is TaskOutcome.QUESTION and not question:
            raise ContractError("Question results require a question")
        if answer and self.outcome is not TaskOutcome.QUESTION:
            raise ContractError("Only question results may contain an answer")
        return TaskResult(
            self.outcome,
            cast(Mapping[str, object], bounded_evidence),
            tuple(references),
            question,
            required_action,
            validation_reason,
            answer,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "outcome": self.outcome.value,
            "evidence": _bounded_value(self.evidence),
            "artifact_refs": [
                _bounded_value(reference) for reference in self.artifact_refs
            ],
            "question": self.question,
            "required_action": self.required_action,
            "validation_reason": self.validation_reason,
            "answer": self.answer,
        }
