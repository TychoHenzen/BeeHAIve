from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any, cast

from beehaiive.session_evidence import SESSION_MESSAGE_SOURCES

from .constants import _SESSION_PROGRESS_EVENTS
from .values import _nonnegative_int as _nonnegative_int
from .values import _text_value as _text_value
from .worker_text import redact_worker_text as redact_worker_text


class CodexSessionMixin:
    def _record_session_line(
        self: Any,
        line: str,
        handler: Callable[[str, str, str | None, str], None],
    ) -> None:
        try:
            raw: object = json.loads(line)
        except json.JSONDecodeError:
            raw = None
        if not isinstance(raw, dict):
            handler("gap", "malformed", None, "")
            return
        payload = cast(dict[str, object], raw)
        if not isinstance(payload.get("type"), str):
            handler("gap", "malformed", None, "")
            return
        event = self._session_event(line)
        if event is None:
            if payload.get("type") == "agent_message":
                handler("gap", "malformed", None, "")
            return
        kind, source_type, role, text = event
        handler(
            kind,
            source_type,
            role,
            redact_worker_text(text, self._secret_values, max_length=None),
        )

    @staticmethod
    def _session_event(
        line: str,
    ) -> tuple[str, str, str | None, str] | None:
        try:
            raw_event: object = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(raw_event, dict):
            return None
        event = cast(dict[str, object], raw_event)
        event_type = event.get("type")
        item_value = event.get("item")
        item = (
            cast(dict[str, object], item_value) if isinstance(item_value, dict) else {}
        )
        if (
            item.get("type") == "agent_message"
            and isinstance(event_type, str)
            and event_type in SESSION_MESSAGE_SOURCES
        ):
            message = _session_message(item.get("text", item.get("content")))
            if message:
                return "message", event_type, "assistant", message
            return "gap", "malformed", None, ""
        if event_type == "agent_message":
            message = _session_message(event.get("text", event.get("content")))
            if message:
                return "message", "agent_message", "assistant", message
        if isinstance(event_type, str) and event_type in _SESSION_PROGRESS_EVENTS:
            return "progress", event_type, None, event_type
        return None

    @staticmethod
    def _parse_output(output: str) -> tuple[str, int, int]:
        final_message = ""
        fallback_lines: list[str] = []
        input_tokens = 0
        output_tokens = 0
        for line in output.splitlines():
            try:
                raw_event: object = json.loads(line)
            except json.JSONDecodeError:
                fallback_lines.append(line)
                continue
            if not isinstance(raw_event, dict):
                continue
            event = cast(dict[str, object], raw_event)
            usage_value = event.get("usage")
            usage = (
                cast(dict[str, object], usage_value)
                if isinstance(usage_value, dict)
                else None
            )
            if isinstance(usage, Mapping):
                input_tokens = _nonnegative_int(usage.get("input_tokens"), input_tokens)
                output_tokens = _nonnegative_int(
                    usage.get("output_tokens"), output_tokens
                )
            item_value = event.get("item")
            item = (
                cast(dict[str, object], item_value)
                if isinstance(item_value, dict)
                else None
            )
            if isinstance(item, Mapping) and item.get("type") == "agent_message":
                message = _text_value(item.get("text", item.get("content")))
                if message:
                    final_message = message
            elif event.get("type") == "agent_message":
                message = _text_value(event.get("text", event.get("content")))
                if message:
                    final_message = message
        if not final_message:
            final_message = "\n".join(fallback_lines).strip()
        if input_tokens == 0:
            input_tokens = max(1, len(output) // 4)
        if output_tokens == 0:
            output_tokens = max(1, len(final_message) // 4)
        return final_message, input_tokens, output_tokens


__all__ = ["CodexSessionMixin"]


def _session_message(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts: list[str] = []
        for raw in cast(list[object], value):
            if not isinstance(raw, dict):
                continue
            part = cast(dict[str, object], raw)
            if part.get("type") not in ("text", "output_text"):
                continue
            text = part.get("text")
            if isinstance(text, str):
                parts.append(text)
        return "\n".join(parts).strip()
    return ""
