from __future__ import annotations

from beehaiive.outcomes import clean, first_sentence, issue_in, stage_name, stop_reason


def test_clean_drops_the_skill_banner_and_markdown() -> None:
    banner = "Skill loaded: dod-guard:next-ticket (v2). **Done**: `pytest` passes."
    assert clean(banner) == "Done: pytest passes."
    assert clean("plain\n\n  text") == "plain text"


def test_first_sentence_stops_at_the_first_full_stop_and_caps_length() -> None:
    assert first_sentence("One. Two.") == "One."
    assert first_sentence("No stop at all") == "No stop at all"
    assert first_sentence("x" * 50, limit=10) == "xxxxxxx..."
    assert first_sentence("") == ""


def test_stop_reason_reads_the_reported_reason() -> None:
    assert stop_reason("Stopped. Stop reason: CI is red. Next: fix it.") == "CI is red"
    assert stop_reason("**Stop reason** - duplicate evidence") == "duplicate evidence"
    assert stop_reason("Stop reason: none.") is None
    assert stop_reason("Stop reason: N/A") is None
    assert stop_reason("All checks passed.") is None


def test_issue_in_prefers_a_hash_number_and_skips_dates() -> None:
    assert issue_in("review 12 then #405") == 405
    assert issue_in("next-ticket 405") == 405
    assert issue_in("Friction log 2026-11-05") is None
    assert issue_in("bump to 1.20.3 at 10:30") is None
    assert issue_in("Refresh queue") is None


def test_stage_name_drops_the_issue_and_filler_words() -> None:
    assert (
        stage_name("fix-pr-review for PR 434 round 2", 434) == "fix-pr-review round 2"
    )
    assert stage_name("Write the tests", None) == "Write tests"
    assert stage_name("for the", None) == "for the"
