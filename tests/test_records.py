from __future__ import annotations

from pathlib import Path

from beehaiive.records import (
    TranscriptTail,
    dig,
    get_int,
    parse_time,
    read_json_file,
    text_of,
    type_markers,
)


def test_tail_returns_only_complete_new_lines(tmp_path: Path) -> None:
    path = tmp_path / "s.jsonl"
    path.write_bytes(b'{"type":"user","n":1}\n{"type":"us')
    tail = TranscriptTail(path)

    assert tail.read_new() == [{"type": "user", "n": 1}]

    with path.open("ab") as handle:
        handle.write(b'er","n":2}\n')
    assert tail.read_new() == [{"type": "user", "n": 2}]
    assert tail.read_new() == []


def test_tail_skips_lines_without_a_marker_and_counts_bad_json(tmp_path: Path) -> None:
    path = tmp_path / "s.jsonl"
    path.write_bytes(
        b'{"type":"progress"}\n'
        b'{"type": "user"}\n'
        b'"type":"user" not json\n'
        b'["type":"user"]\n'
        b"\n"
    )
    tail = TranscriptTail(path, type_markers("user"))

    assert tail.read_new() == [{"type": "user"}]
    # The progress line is skipped unparsed; the two broken user lines count.
    assert tail.unreadable == 2


def test_tail_counts_json_that_is_not_an_object(tmp_path: Path) -> None:
    path = tmp_path / "s.jsonl"
    path.write_bytes(b'[1, 2]\n"text"\n')
    tail = TranscriptTail(path)

    assert tail.read_new() == []
    assert tail.unreadable == 2


def test_tail_starts_over_when_the_file_shrinks(tmp_path: Path) -> None:
    path = tmp_path / "s.jsonl"
    path.write_bytes(b'{"n":1}\n{"n":2}\n')
    tail = TranscriptTail(path)
    tail.read_new()

    path.write_bytes(b'{"n":3}\n')

    assert tail.read_new() == [{"n": 3}]
    assert tail.was_reset
    tail.read_new()
    assert not tail.was_reset


def test_tail_of_a_missing_file_is_empty(tmp_path: Path) -> None:
    tail = TranscriptTail(tmp_path / "gone.jsonl")

    assert tail.read_new() == []
    assert tail.offset == 0


def test_field_helpers_tolerate_wrong_types() -> None:
    record: dict[str, object] = {"a": {"b": {"c": 3}}, "flag": True, "n": "7"}

    assert dig(record, "a", "b", "c") == 3
    assert dig(record, "a", "x", "c") is None
    assert get_int(record, "flag") == 0
    assert get_int(record, "n") == 0
    assert get_int(None, "n") == 0
    assert parse_time("not a time") is None
    assert parse_time(None) is None
    parsed = parse_time("2026-11-05T12:00:00Z")
    assert parsed is not None and parsed.tzinfo is not None


def test_text_of_joins_text_blocks() -> None:
    blocks: list[object] = [
        {"type": "text", "text": "one"},
        {"type": "tool_result", "content": "two"},
        {"type": "image"},
        "stray",
    ]

    assert text_of(blocks) == "one\ntwo"
    assert text_of("plain") == "plain"
    assert text_of(None) == ""


def test_read_json_file_returns_none_for_bad_files(tmp_path: Path) -> None:
    good = tmp_path / "good.json"
    good.write_text('{"x": 1}', encoding="utf-8")
    bad = tmp_path / "bad.json"
    bad.write_text("{", encoding="utf-8")

    assert read_json_file(good) == {"x": 1}
    assert read_json_file(bad) is None
    assert read_json_file(tmp_path / "missing.json") is None
