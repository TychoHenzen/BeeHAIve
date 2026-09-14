from __future__ import annotations

import json
import math
from pathlib import Path
from typing import cast

from ..agent import (
    MAX_AGENT_OUTPUT_LENGTH,
    redact_worker_text,
)
from ..workflow import CheckResult
from .gate_definition import GateDefinition
from .quality_gate_constants import (
    CATEGORY,
    GATE_KEYS,
    GATE_NAME,
    MANIFEST_NAME,
    MAX_GATE_COUNT,
    MAX_GATE_TIMEOUT_SECONDS,
    MAX_MANIFEST_BYTES,
    ROOT_KEYS,
)


def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate manifest key: {key}")
        result[key] = value
    return result


def parse_manifest(value: object) -> tuple[GateDefinition, ...]:
    if not isinstance(value, dict):
        raise ValueError("Manifest must contain only version and gates")
    root = cast(dict[str, object], value)
    if set(root) != ROOT_KEYS:
        raise ValueError("Manifest must contain only version and gates")
    version = root.get("version")
    if type(version) is not int or version != 1:
        raise ValueError("Manifest version must be 1")
    raw_gates = root.get("gates")
    if not isinstance(raw_gates, list):
        raise ValueError(f"Manifest must declare 1 to {MAX_GATE_COUNT} gates")
    gate_values = cast(list[object], raw_gates)
    if not 1 <= len(gate_values) <= MAX_GATE_COUNT:
        raise ValueError(f"Manifest must declare 1 to {MAX_GATE_COUNT} gates")

    gates: list[GateDefinition] = []
    names: set[str] = set()
    for index, gate_value in enumerate(gate_values):
        if not isinstance(gate_value, dict):
            raise ValueError(f"Gate {index + 1} has missing or unknown fields")
        raw_gate = cast(dict[str, object], gate_value)
        if set(raw_gate) != GATE_KEYS:
            raise ValueError(f"Gate {index + 1} has missing or unknown fields")
        name = raw_gate.get("name")
        argv = raw_gate.get("argv")
        timeout = raw_gate.get("timeout_seconds")
        category = raw_gate.get("category")
        required = raw_gate.get("required")
        external_only = raw_gate.get("external_only")
        if not isinstance(name, str) or not GATE_NAME.fullmatch(name):
            raise ValueError(f"Gate {index + 1} has an invalid name")
        if name in names:
            raise ValueError(f"Gate name is duplicated: {name}")
        names.add(name)
        if not isinstance(argv, list):
            raise ValueError(f"Gate {name} has an invalid argument vector")
        arguments = cast(list[object], argv)
        if len(arguments) > 64 or any(
            not isinstance(argument, str)
            or not argument.strip()
            or "\x00" in argument
            or len(argument) > 4_096
            for argument in arguments
        ):
            raise ValueError(f"Gate {name} has an invalid argument vector")
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool):
            raise ValueError(f"Gate {name} timeout must be a number")
        try:
            timeout_seconds = float(timeout)
        except OverflowError as exc:
            raise ValueError(
                f"Gate {name} timeout exceeds the supported range"
            ) from exc
        if (
            not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
            or timeout_seconds > MAX_GATE_TIMEOUT_SECONDS
        ):
            raise ValueError(
                f"Gate {name} timeout must be greater than 0 and no more than "
                f"{MAX_GATE_TIMEOUT_SECONDS:g} seconds"
            )
        if not isinstance(category, str) or not CATEGORY.fullmatch(category):
            raise ValueError(f"Gate {name} has an invalid category")
        if type(required) is not bool or type(external_only) is not bool:
            raise ValueError(f"Gate {name} required and external_only must be booleans")
        required_value = required
        external_only_value = external_only
        if external_only_value:
            if arguments or required_value:
                raise ValueError(
                    f"External-only gate {name} must have an empty argv and be optional"
                )
        elif not arguments:
            raise ValueError(f"Local gate {name} needs an argument vector")
        gates.append(
            GateDefinition(
                name,
                tuple(cast(str, argument) for argument in arguments),
                timeout_seconds,
                category,
                required_value,
                external_only_value,
            )
        )
    return tuple(gates)


def configuration_result(
    status: str, message: str, secret_values: tuple[str, ...]
) -> CheckResult:
    error = redact_worker_text(message, secret_values)
    return CheckResult(
        "gate_manifest",
        False,
        error,
        status=status,
        category="configuration",
        required=True,
        error=error,
    )


def load_manifest(
    workspace: Path, secret_values: tuple[str, ...]
) -> tuple[GateDefinition, ...] | CheckResult:
    manifest = workspace / MANIFEST_NAME
    try:
        if manifest.is_symlink() or not manifest.resolve().is_relative_to(
            workspace.resolve()
        ):
            raise ValueError(
                "Manifest must be a regular file inside the leased checkout"
            )
        if manifest.stat().st_size > MAX_MANIFEST_BYTES:
            raise ValueError(f"Manifest exceeds {MAX_MANIFEST_BYTES} bytes")
        decoded = json.loads(manifest.read_bytes(), object_pairs_hook=unique_object)
        return parse_manifest(decoded)
    except FileNotFoundError:
        return configuration_result(
            "configuration_missing",
            f"Required root manifest {MANIFEST_NAME} is missing",
            secret_values,
        )
    except (OSError, ValueError, UnicodeDecodeError, RecursionError) as exc:
        message = (
            str(exc)
            if isinstance(exc, (ValueError, RecursionError))
            else "Manifest cannot be read"
        )
        return configuration_result("invalid_configuration", message, secret_values)


def redact(text: str, workspace: Path, secrets: tuple[str, ...]) -> str:
    root = str(workspace.resolve())
    for path in {root, root.replace("\\", "/"), root.replace("/", "\\")}:
        text = text.replace(path, "<workspace>")
    return redact_worker_text(text, secrets, max_length=MAX_AGENT_OUTPUT_LENGTH)
