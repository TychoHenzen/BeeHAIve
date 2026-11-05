"""Build Claude Code transcript records and files for tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from beehaiive.records import Record

BASE = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)


def at(seconds: float) -> datetime:
    return BASE + timedelta(seconds=seconds)


def ts(seconds: float) -> str:
    return at(seconds).isoformat().replace("+00:00", "Z")


def human(text: str, seconds: float, uuid: str = "") -> Record:
    record: Record = {
        "type": "user",
        "timestamp": ts(seconds),
        "cwd": "C:/work/repo",
        "gitBranch": "main",
        "message": {"role": "user", "content": text},
    }
    if uuid:
        record["uuid"] = uuid
    return record


def tool_use(
    tool_id: str,
    name: str,
    tool_input: Record,
    seconds: float,
    message_id: str = "",
    usage: Record | None = None,
) -> Record:
    message: Record = {
        "id": message_id or f"msg-{tool_id}",
        "model": "claude-test",
        "content": [
            {"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}
        ],
    }
    if usage is not None:
        message["usage"] = usage
    return {"type": "assistant", "timestamp": ts(seconds), "message": message}


def text(body: str, seconds: float) -> Record:
    return {
        "type": "assistant",
        "timestamp": ts(seconds),
        "message": {
            "id": f"text-{seconds}",
            "content": [{"type": "text", "text": body}],
        },
    }


def tool_result(
    tool_id: str,
    seconds: float,
    content: str = "ok",
    is_error: bool = False,
    result: Record | None = None,
) -> Record:
    record: Record = {
        "type": "user",
        "timestamp": ts(seconds),
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": content,
                    "is_error": is_error,
                }
            ],
        },
    }
    if result is not None:
        record["toolUseResult"] = result
    return record


def notification(
    tool_id: str, seconds: float, status: str, summary: str = ""
) -> Record:
    body = (
        f"<task-notification><tool-use-id>{tool_id}</tool-use-id>"
        f"<status>{status}</status><summary>{summary}</summary></task-notification>"
    )
    return {
        "type": "user",
        "timestamp": ts(seconds),
        "message": {"role": "user", "content": body},
    }


def write_jsonl(path: Path, records: list[Record]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def append_jsonl(path: Path, records: list[Record]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")
