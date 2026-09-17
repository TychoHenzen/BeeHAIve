"""Allowed session evidence shared by capture, persistence, and analysis."""

from collections.abc import Mapping, Sequence
from typing import cast

from .contract_types.validation import _redact_text

SESSION_PROGRESS_EVENTS = frozenset(
    {
        "thread.started",
        "turn.started",
        "turn.completed",
        "turn.failed",
        "item.started",
        "item.updated",
        "item.completed",
    }
)
SESSION_MESSAGE_SOURCES = frozenset(
    {"agent_message", "item.started", "item.updated", "item.completed"}
)
CAPTURE_GAPS = {
    "malformed": 1,
    "oversized": 2,
    "truncated": 4,
    "trimmed": 8,
    "interrupted": 16,
}
MAX_TRANSCRIPT_EVENTS = 20
MAX_TRANSCRIPT_EXCERPT = 500


def allowed_session_event(kind: object, source: object, role: object) -> bool:
    if not isinstance(source, str):
        return False
    return (
        kind == "message" and source in SESSION_MESSAGE_SOURCES and role == "assistant"
    ) or (kind == "progress" and source in SESSION_PROGRESS_EVENTS and role is None)


def transcript_projection(
    run_id: str,
    events: object,
    gaps: object = (),
) -> dict[str, object]:
    """Revalidate retained events without trusting provider metadata or raw fields."""
    diagnostics = {
        "partial: bounded capture does not establish transcript completeness"
    }
    if isinstance(gaps, Sequence) and not isinstance(gaps, (str, bytes)):
        diagnostics.update(
            gap
            for gap in cast(Sequence[object], gaps)
            if isinstance(gap, str) and gap in CAPTURE_GAPS
        )
    if not isinstance(events, Sequence) or isinstance(events, (str, bytes)):
        events = ()
        diagnostics.add("missing")
    entries: dict[int, dict[str, object]] = {}
    duplicates: set[int] = set()
    for raw in cast(Sequence[object], events):
        if not isinstance(raw, Mapping):
            diagnostics.add("malformed")
            continue
        event = cast(Mapping[str, object], raw)
        sequence = event.get("sequence")
        text = event.get("text")
        timestamp = event.get("timestamp")
        if (
            type(sequence) is not int
            or sequence <= 0
            or not isinstance(text, str)
            or not isinstance(timestamp, str)
            or not allowed_session_event(
                event.get("kind"), event.get("source_type"), event.get("role")
            )
        ):
            diagnostics.add("malformed")
            continue
        if sequence in entries or sequence in duplicates:
            entries.pop(sequence, None)
            duplicates.add(sequence)
            diagnostics.add("malformed")
            continue
        source = str(event["source_type"])
        safe_text = (
            _redact_text(text, len(text)) if event["kind"] == "message" else source
        )
        if len(safe_text) > MAX_TRANSCRIPT_EXCERPT:
            diagnostics.add("truncated")
        entries[sequence] = {
            "source_id": f"run:{_redact_text(run_id, 120)}:transcript:{sequence}",
            "sequence": sequence,
            "kind": event["kind"],
            "source_type": source,
            "role": event["role"],
            "text": safe_text[:MAX_TRANSCRIPT_EXCERPT],
            "timestamp": _redact_text(timestamp, 80),
        }
    sequences = sorted(entries)
    if not sequences:
        diagnostics.add("missing")
    elif sequences[0] > 1 or any(
        b != a + 1 for a, b in zip(sequences, sequences[1:], strict=False)
    ):
        diagnostics.add("trimmed")
    if len(sequences) > MAX_TRANSCRIPT_EVENTS:
        diagnostics.add("trimmed")
    return {
        "events": [
            entries[sequence] for sequence in sequences[-MAX_TRANSCRIPT_EVENTS:]
        ],
        "gaps": sorted(diagnostics),
    }
