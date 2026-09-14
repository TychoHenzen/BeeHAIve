from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import cast

from .artifact_requirement import ArtifactRequirement
from .constants import MAX_CONTRACT_ITEMS
from .contract_error import ContractError
from .task_outcome import TaskOutcome
from .validation import _bounded_value, _string_tuple, _text, _validate_unique_strings

__all__ = ["TaskContract"]


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
