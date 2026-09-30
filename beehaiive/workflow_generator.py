from __future__ import annotations

import json
import re
import subprocess
import tempfile
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from .config import CoreConfig, CoreConfigurationError
from .project import ProjectDataError
from .rest import GithubRestError
from .snapshot import ProjectSnapshotService
from .workflows import assign_layered_layout, validate_workflow

GENERATOR_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "schema_version": {"type": "integer"},
        "name": {"type": "string"},
        "description": {"type": "string"},
        "auto_reset_on_stall": {"type": "boolean"},
        "max_steps_per_pass": {"type": "integer"},
        "parameters": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "name": {"type": "string"},
                    "type": {"type": "string"},
                    "const": {"type": "boolean"},
                    "value": {"type": ["string", "number", "boolean", "null"]},
                },
                "required": ["name", "type", "const", "value"],
            },
        },
        "initial": {"type": "string"},
        "states": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string"},
                    "title": {"type": "string"},
                    "action": {"type": "string"},
                    "skill": {"type": ["string", "null"]},
                    "prompt": {"type": ["string", "null"]},
                    "outcomes": {"type": "array", "items": {"type": "string"}},
                    "max_visits": {"type": "integer"},
                },
                "required": [
                    "id",
                    "title",
                    "action",
                    "skill",
                    "prompt",
                    "outcomes",
                    "max_visits",
                ],
            },
        },
        "transitions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "from": {"type": "string"},
                    "to": {"type": "string"},
                    "priority": {"type": "integer"},
                    "conditions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "kind": {"type": "string"},
                                "value": {
                                    "type": ["string", "number", "boolean", "null"]
                                },
                            },
                            "required": ["kind", "value"],
                        },
                    },
                },
                "required": ["from", "to", "priority", "conditions"],
            },
        },
    },
    "required": [
        "schema_version",
        "name",
        "description",
        "auto_reset_on_stall",
        "max_steps_per_pass",
        "parameters",
        "initial",
        "states",
        "transitions",
    ],
}

_PLACEHOLDER = re.compile(r"\{([a-z][a-z0-9_]*)\}")
CODEX_TIMEOUT_SECONDS = 300

WORKFLOW_SEMANTIC_CONTRACT = (
    "Parameter names match [a-z][a-z0-9_]{0,31}; parameter types are status, "
    "label, skill, repository, item_type, text, number, or boolean.",
    "A const parameter must have a non-null value of its declared type; a "
    "non-const parameter has value null. Item types are issue or pull_request.",
    "Placeholders are exactly {parameter_name}, must be declared, and must use "
    "the type required by their field or condition; source and state prompts "
    "use text parameters.",
    "Condition kinds are item_status_is, item_type_is, item_has_label, "
    "item_lacks_label, item_repository_is, outcome_is, and always.",
    "always omits value (or uses null); every other condition has a non-empty "
    "string literal or a typed placeholder. Status literals must be known "
    "Project Status options when options are available; otherwise report a "
    "warning rather than inventing an option.",
    "The initial state is wait_for_work. wait_for_work edges use item conditions "
    "and never outcome_is; run_skill declares outcomes and has one outcome_is "
    "edge for each; escalate has exactly one always edge.",
    "Every state is reachable from initial and can reach initial; transition "
    "priorities are integers. Literal skills must resolve to a configured "
    "<skill>/SKILL.md.",
    "Prompts are non-empty and at most 8 KB. Limits are 32 parameters, 32 states, "
    "128 transitions, and 100 max steps per pass.",
)


Runner = Callable[[list[str], str, Path], str]


class WorkflowGenerationService:
    def __init__(
        self,
        config: CoreConfig,
        project_service: ProjectSnapshotService,
        runner: Runner | None = None,
    ) -> None:
        self.config = config
        self.project_service = project_service
        self._runner = runner or _run_codex
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()

    def start(
        self,
        name: str,
        prompt: str,
        parameters: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        job_id = uuid.uuid4().hex
        job = {
            "id": job_id,
            "status": "pending",
            "name": name,
            "prompt": prompt,
            "parameters": [dict(parameter) for parameter in parameters],
            "draft": None,
            "validation_errors": [],
            "warnings": [],
            "attempts": 0,
            "error": None,
        }
        with self._lock:
            self._jobs[job_id] = job
        thread = threading.Thread(
            target=self._run_job,
            args=(job_id,),
            daemon=True,
            name=f"beehaiive-workflow-{job_id[:8]}",
        )
        thread.start()
        return {"id": job_id, "status": "pending"}

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return dict(job) if job is not None else None

    def _run_job(self, job_id: str) -> None:
        try:
            with self._lock:
                job = self._jobs[job_id]
            name = str(job["name"])
            prompt = str(job["prompt"])
            parameters = cast(list[Mapping[str, Any]], job["parameters"])
            catalog = discover_skill_catalog(self.config.skills_dirs)
            statuses, labels = self._project_metadata()
            generator_prompt = build_generator_prompt(
                name=name,
                operator_prompt=prompt,
                parameters=parameters,
                skills=catalog,
                statuses=statuses,
                labels=labels,
            )
            definition: dict[str, Any] | None = None
            validation_errors: list[dict[str, str]] = []
            warnings: list[dict[str, str]] = []
            for attempt in range(2):
                command_prompt = generator_prompt
                if attempt:
                    command_prompt += (
                        "\n\nRepair these validation errors exactly once:\n"
                    )
                    command_prompt += json.dumps(validation_errors, indent=2)
                raw = self._invoke(command_prompt)
                definition = _decode_definition(raw)
                definition = _complete_generated_definition(
                    definition,
                    name=name,
                    operator_prompt=prompt,
                    parameters=parameters,
                )
                validation = validate_workflow(
                    definition,
                    skills_dirs=self.config.skills_dirs,
                    status_options=statuses,
                )
                definition = validation.definition
                validation_errors = [issue.as_dict() for issue in validation.errors]
                warnings = [issue.as_dict() for issue in validation.warnings]
                with self._lock:
                    self._jobs[job_id]["attempts"] = attempt + 1
                    self._jobs[job_id]["draft"] = definition
                    self._jobs[job_id]["validation_errors"] = validation_errors
                    self._jobs[job_id]["warnings"] = warnings
                if validation.valid:
                    definition = assign_layered_layout(definition)
                    with self._lock:
                        self._jobs[job_id]["draft"] = definition
                        self._jobs[job_id]["status"] = "succeeded"
                    return
            with self._lock:
                self._jobs[job_id]["status"] = "failed"
        except Exception as error:  # noqa: BLE001 - background jobs must report failure
            with self._lock:
                self._jobs[job_id]["status"] = "failed"
                self._jobs[job_id]["error"] = str(error)

    def _invoke(self, prompt: str) -> str:
        with tempfile.TemporaryDirectory(prefix="beehaiive-workflow-") as directory:
            directory_path = Path(directory)
            schema_path = directory_path / "schema.json"
            result_path = directory_path / "result.json"
            schema_path.write_text(
                json.dumps(GENERATOR_OUTPUT_SCHEMA, separators=(",", ":")),
                encoding="utf-8",
            )
            command = [
                self.config.codex,
                "exec",
                "--sandbox",
                "read-only",
                "--ephemeral",
                "--skip-git-repo-check",
                "--output-schema",
                str(schema_path),
                "-o",
                str(result_path),
                "-",
            ]
            output = self._runner(command, prompt, result_path)
            if output.strip():
                return output
            if result_path.is_file():
                return result_path.read_text(encoding="utf-8")
            raise RuntimeError("codex exec produced no structured output")

    def _project_metadata(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        try:
            snapshot = self.project_service.get_snapshot()
        except (CoreConfigurationError, GithubRestError, ProjectDataError, OSError):
            return (), ()
        statuses = tuple(
            column.status for column in snapshot.columns if column.status != "No status"
        )
        labels = tuple(
            sorted(
                {
                    label
                    for column in snapshot.columns
                    for card in column.items
                    for label in card.labels
                }
            )
        )
        return statuses, labels


def build_generator_prompt(
    *,
    name: str,
    operator_prompt: str,
    parameters: Sequence[Mapping[str, Any]],
    skills: Sequence[Mapping[str, str]],
    statuses: Sequence[str],
    labels: Sequence[str],
) -> str:
    entered = [dict(parameter) for parameter in parameters]
    known = {str(parameter.get("name")) for parameter in entered}
    for placeholder in _PLACEHOLDER.findall(operator_prompt):
        if placeholder not in known:
            entered.append(
                {
                    "name": placeholder,
                    "type": _infer_parameter_type(placeholder),
                    "const": False,
                    "value": None,
                }
            )
            known.add(placeholder)
    return "\n".join(
        (
            "Generate one schema-version 1 BeeHAIve workflow definition as JSON.",
            "Do not include markdown fences or properties outside the output schema.",
            "The definition is validated server-side; use wait_for_work as initial.",
            f"Workflow name: {name}",
            f"Operator prompt:\n{operator_prompt}",
            "Parameter table (preserve entered names and add inferred placeholders):",
            json.dumps(entered, indent=2),
            "Available skills (name and SKILL.md description):",
            json.dumps(list(skills), indent=2),
            f"Known Project Status options: {json.dumps(list(statuses))}",
            f"Known labels: {json.dumps(list(labels))}",
            *WORKFLOW_SEMANTIC_CONTRACT,
        )
    )


def discover_skill_catalog(skills_dirs: Sequence[Path]) -> tuple[dict[str, str], ...]:
    catalog: list[dict[str, str]] = []
    seen: set[str] = set()
    for directory in skills_dirs:
        if not directory.is_dir():
            continue
        for skill_file in sorted(directory.glob("*/SKILL.md")):
            name = skill_file.parent.name
            if name in seen:
                continue
            seen.add(name)
            catalog.append(
                {"name": name, "description": _skill_description(skill_file)}
            )
    return tuple(catalog)


def _skill_description(path: Path) -> str:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    in_front_matter = False
    for line in lines:
        if line.strip() == "---":
            if in_front_matter:
                break
            in_front_matter = True
            continue
        if in_front_matter and line.startswith("description:"):
            return line.partition(":")[2].strip().strip("\"'")
    return ""


def _run_codex(command: list[str], prompt: str, result_path: Path) -> str:
    try:
        result = subprocess.run(
            command,
            input=prompt,
            capture_output=True,
            text=True,
            check=False,
            timeout=CODEX_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(
            f"codex exec timed out after {CODEX_TIMEOUT_SECONDS} seconds"
        ) from error
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "codex exec failed"
        raise RuntimeError(message)
    if result_path.is_file():
        return result_path.read_text(encoding="utf-8")
    if result.stdout.strip():
        return result.stdout
    raise RuntimeError("codex exec produced no structured output")


def _decode_definition(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        raise RuntimeError("codex output was not valid JSON") from error
    if isinstance(value, dict):
        value_object = cast(dict[str, Any], value)
        if isinstance(value_object.get("definition"), dict):
            value = value_object["definition"]
    if not isinstance(value, dict):
        raise RuntimeError("codex output was not a workflow object")
    return cast(dict[str, Any], value)


def _complete_generated_definition(
    definition: dict[str, Any],
    *,
    name: str,
    operator_prompt: str,
    parameters: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    result = dict(definition)
    result.setdefault("name", name)
    result["source_prompt"] = operator_prompt
    generated_parameters = result.get("parameters")
    generated_values = (
        cast(list[Any], generated_parameters)
        if isinstance(generated_parameters, list)
        else []
    )
    merged: list[dict[str, Any]] = []
    names: set[str] = set()
    for parameter in parameters:
        parameter_copy = dict(parameter)
        parameter_name = str(parameter_copy.get("name", ""))
        if parameter_name not in names:
            merged.append(parameter_copy)
            names.add(parameter_name)
    for parameter in generated_values:
        if not isinstance(parameter, Mapping):
            continue
        generated = dict(cast(Mapping[str, Any], parameter))
        parameter_name = str(generated.get("name", ""))
        if parameter_name not in names:
            merged.append(generated)
            names.add(parameter_name)
    known = set(names)
    for placeholder in _PLACEHOLDER.findall(operator_prompt):
        if placeholder not in known:
            merged.append(
                {
                    "name": placeholder,
                    "type": _infer_parameter_type(placeholder),
                    "const": False,
                    "value": None,
                }
            )
            known.add(placeholder)
    result["parameters"] = merged
    return result


def _infer_parameter_type(name: str) -> str:
    lowered = name.casefold()
    if "status" in lowered:
        return "status"
    if "label" in lowered:
        return "label"
    if "skill" in lowered:
        return "skill"
    if "repo" in lowered:
        return "repository"
    if lowered in {"type", "item_type", "kind"}:
        return "item_type"
    if lowered.startswith("is_") or lowered.startswith("has_"):
        return "boolean"
    if lowered.endswith("_number") or lowered.endswith("_count"):
        return "number"
    return "text"
