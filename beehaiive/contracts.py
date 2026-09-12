"""Versioned worker contracts and structured terminal outcomes."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import cast


class ContractError(ValueError):
    """Raised when a worker contract or result is not safe to use."""


class TaskOutcome(StrEnum):
    """Terminal outcome of one executable worker step."""

    PASS = "pass"
    FAIL = "fail"
    BLOCKED = "blocked"
    QUESTION = "question"


MAX_CONTRACT_ITEMS = 20
MAX_CONTRACT_TEXT = 1_000
MAX_RESULT_TEXT = 4_000
MAX_RESULT_KEYS = 40
MAX_RESULT_DEPTH = 4
_SECRET_JSON = re.compile(
    r"([\"']?(?:access[_-]?token|refresh[_-]?token|token|api[_-]?key|"
    r"client[_-]?secret|secret|password)[\"']?\s*:\s*)"
    r"(?:\"[^\"]*\"|'[^']*'|[^,}\s]+)",
    re.IGNORECASE,
)
_SECRET_ASSIGNMENT = re.compile(
    r"\b(token|api[_-]?key|secret|password)\b\s*[:=]\s*\S+", re.IGNORECASE
)
_BEARER_TOKEN = re.compile(r"\bBearer\s+\S+", re.IGNORECASE)
_URL_CREDENTIALS = re.compile(r"(https?://)[^/\s:@]+:[^@\s]+@", re.IGNORECASE)


def _redact_text(value: str, limit: int = MAX_RESULT_TEXT) -> str:
    redacted = _SECRET_JSON.sub(r"\1[redacted]", value)
    redacted = _BEARER_TOKEN.sub("Bearer [redacted]", redacted)
    redacted = _URL_CREDENTIALS.sub(r"\1[redacted]@", redacted)
    redacted = _SECRET_ASSIGNMENT.sub(
        lambda match: f"{match.group(1)}=[redacted]", redacted
    )
    return redacted.strip()[:limit]


def _bounded_value(value: object, depth: int = 0) -> object:
    if depth > MAX_RESULT_DEPTH:
        raise ContractError("Result data is nested too deeply")
    if isinstance(value, str):
        return _redact_text(value)
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ContractError("Result data contains a non-finite number")
        return value
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        if len(mapping) > MAX_RESULT_KEYS:
            raise ContractError("Result data contains too many keys")
        bounded: dict[str, object] = {}
        for key, item in mapping.items():
            if not isinstance(key, str) or not key.strip():
                raise ContractError("Result data keys must be non-empty strings")
            bounded[key[:MAX_CONTRACT_TEXT]] = _bounded_value(item, depth + 1)
        return bounded
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        sequence = cast(Sequence[object], value)
        if len(sequence) > MAX_CONTRACT_ITEMS:
            raise ContractError("Result data contains too many items")
        return [_bounded_value(item, depth + 1) for item in sequence]
    raise ContractError("Result data must contain JSON-compatible values")


def _text(value: object, field_name: str, *, required: bool = True) -> str | None:
    if not isinstance(value, str):
        if required:
            raise ContractError(f"{field_name} must be a string")
        return None
    normalized = _redact_text(value, MAX_CONTRACT_TEXT)
    if required and not normalized:
        raise ContractError(f"{field_name} is required")
    return normalized or None


@dataclass(frozen=True, slots=True)
class ArtifactRequirement:
    """One artifact that a task may require from the worker."""

    artifact_id: str
    description: str = ""
    required: bool = True

    def __post_init__(self) -> None:
        if not self.artifact_id.strip():
            raise ContractError("Artifact id is required")
        if len(self.artifact_id) > MAX_CONTRACT_TEXT:
            raise ContractError("Artifact id is too long")
        if len(self.description) > MAX_CONTRACT_TEXT:
            raise ContractError("Artifact description is too long")

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.artifact_id,
            "description": _redact_text(self.description, MAX_CONTRACT_TEXT),
            "required": self.required,
        }

    @classmethod
    def from_value(cls, value: object) -> ArtifactRequirement:
        if not isinstance(value, Mapping):
            raise ContractError("Artifact requirements must be objects")
        mapping = cast(Mapping[str, object], value)
        artifact_id = _text(mapping.get("id"), "Artifact id")
        description = _text(
            mapping.get("description", ""), "Artifact description", required=False
        )
        required = mapping.get("required", True)
        if not isinstance(required, bool):
            raise ContractError("Artifact required must be boolean")
        return cls(artifact_id or "", description or "", required)


@dataclass(frozen=True, slots=True)
class TaskContract:
    """Versioned, bounded input and capability contract for one worker step."""

    contract_id: str
    version: int
    step_id: str
    inputs: Mapping[str, object]
    capabilities: tuple[str, ...]
    required_artifacts: tuple[ArtifactRequirement, ...]
    allowed_outcomes: tuple[TaskOutcome, ...]
    required_evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        self.validate()

    @classmethod
    def inventory(
        cls,
        repository: str,
        pbi_number: int,
        title: str,
        *,
        branch: str | None = None,
        tracked_file_count: int | None = None,
        answer: str | None = None,
    ) -> TaskContract:
        inputs: dict[str, object] = {
            "repository": repository,
            "pbi_number": pbi_number,
            "title": title,
        }
        if branch is not None:
            inputs["branch"] = branch
        if tracked_file_count is not None:
            inputs["tracked_file_count"] = tracked_file_count
        if answer is not None:
            inputs["answer"] = answer
        return cls(
            contract_id="beehaiive.worker.inventory",
            version=1,
            step_id="inventory",
            inputs=inputs,
            capabilities=("read_repository",),
            required_artifacts=(),
            allowed_outcomes=tuple(TaskOutcome),
            required_evidence=("repository", "branch", "tracked_file_count")
            if branch is not None and tracked_file_count is not None
            else (),
        )

    @classmethod
    def from_dict(cls, value: object) -> TaskContract:
        if not isinstance(value, Mapping):
            raise ContractError("Task contract must be an object")
        mapping = cast(Mapping[str, object], value)
        contract_id = _text(mapping.get("contract_id"), "Contract id")
        version = mapping.get("version")
        if not isinstance(version, int) or isinstance(version, bool):
            raise ContractError("Contract version must be an integer")
        step_id = _text(mapping.get("step_id"), "Step id")
        inputs = mapping.get("inputs")
        if not isinstance(inputs, Mapping):
            raise ContractError("Contract inputs must be an object")
        capabilities = _string_tuple(
            mapping.get("capabilities"), "Contract capabilities"
        )
        raw_artifacts = mapping.get("required_artifacts")
        if not isinstance(raw_artifacts, Sequence) or isinstance(
            raw_artifacts, (str, bytes, bytearray)
        ):
            raise ContractError("Required artifacts must be a list")
        raw_artifacts = cast(Sequence[object], raw_artifacts)
        artifacts = tuple(
            ArtifactRequirement.from_value(item) for item in raw_artifacts
        )
        raw_outcomes = mapping.get("allowed_outcomes")
        if not isinstance(raw_outcomes, Sequence) or isinstance(
            raw_outcomes, (str, bytes, bytearray)
        ):
            raise ContractError("Allowed outcomes must be a list")
        raw_outcomes = cast(Sequence[object], raw_outcomes)
        outcomes: list[TaskOutcome] = []
        for raw_outcome in raw_outcomes:
            if not isinstance(raw_outcome, str):
                raise ContractError("Allowed outcomes must be strings")
            try:
                outcomes.append(TaskOutcome(raw_outcome))
            except ValueError as exc:
                raise ContractError(f"Unknown task outcome: {raw_outcome}") from exc
        required_evidence = _string_tuple(
            mapping.get("required_evidence", []), "Required evidence"
        )
        return cls(
            contract_id or "",
            version,
            step_id or "",
            cast(Mapping[str, object], inputs),
            capabilities,
            artifacts,
            tuple(outcomes),
            required_evidence,
        )

    def validate(self) -> None:
        if type(self.version) is not int:
            raise ContractError("Contract version must be an integer")
        if self.version != 1:
            raise ContractError(f"Unknown contract version: {self.version}")
        _text(self.contract_id, "Contract id")
        _text(self.step_id, "Step id")
        if not isinstance(cast(object, self.inputs), Mapping):
            raise ContractError("Contract inputs must be an object")
        _bounded_value(self.inputs)
        _validate_unique_strings(self.capabilities, "Contract capabilities")
        if len(self.capabilities) > MAX_CONTRACT_ITEMS:
            raise ContractError("Contract has too many capabilities")
        if len(self.required_artifacts) > MAX_CONTRACT_ITEMS:
            raise ContractError("Contract has too many required artifacts")
        artifact_ids = [artifact.artifact_id for artifact in self.required_artifacts]
        _validate_unique_strings(artifact_ids, "Artifact ids")
        if not self.allowed_outcomes:
            raise ContractError("Contract must allow at least one outcome")
        if len(self.allowed_outcomes) > len(TaskOutcome):
            raise ContractError("Contract allows too many outcomes")
        if len(set(self.allowed_outcomes)) != len(self.allowed_outcomes):
            raise ContractError("Contract outcomes must be unique")
        _validate_unique_strings(self.required_evidence, "Required evidence")

    def as_dict(self) -> dict[str, object]:
        self.validate()
        return {
            "contract_id": self.contract_id,
            "version": self.version,
            "step_id": self.step_id,
            "inputs": _bounded_value(self.inputs),
            "capabilities": list(self.capabilities),
            "required_artifacts": [
                artifact.as_dict() for artifact in self.required_artifacts
            ],
            "allowed_outcomes": [outcome.value for outcome in self.allowed_outcomes],
            "required_evidence": list(self.required_evidence),
        }


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


def _string_tuple(value: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ContractError(f"{field_name} must be a list")
    values: list[str] = []
    for item in cast(Sequence[object], value):
        text = _text(item, field_name)
        values.append(text or "")
    return tuple(values)


def _validate_unique_strings(values: Sequence[str], field_name: str) -> None:
    if any(not value.strip() for value in values):
        raise ContractError(f"{field_name} must contain non-empty strings")
    if len(set(values)) != len(values):
        raise ContractError(f"{field_name} must be unique")
