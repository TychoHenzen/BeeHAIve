from __future__ import annotations

import pytest

from beehaiive.pbi_refinement_mutation import (
    REFINEMENT_SECTION_ORDER,
    PbiRefinementMutationError,
    PbiRefinementUpdateRequest,
    merge_refinement_sections,
)


def _sections() -> dict[str, str]:
    return {name: f"Updated {name}." for name in REFINEMENT_SECTION_ORDER}


def _request(**changes: object) -> PbiRefinementUpdateRequest:
    values: dict[str, object] = {
        "project_id": "owner:7",
        "repository": "owner/repo",
        "pbi_number": 64,
        "sections": _sections(),
        "priority_label": "Prio 5 - Planned",
        "effort_label": "Effort 5 - Large",
        "standard_labels": ("enhancement",),
    }
    values.update(changes)
    return PbiRefinementUpdateRequest(**values)  # type: ignore[arg-type]


def test_refinement_section_merge_preserves_unmanaged_text_and_code_blocks() -> None:
    body = (
        "Keep this preamble.\n\n"
        "## Outcome\nOld outcome.\n\n"
        "## Notes\nKeep this section.\n\n"
        "```markdown\n## Scope\nThis is literal text.\n```\n"
    )

    merged = merge_refinement_sections(body, _sections())

    assert merged.startswith("Keep this preamble.\n\n## Outcome\nUpdated Outcome.")
    assert "## Notes\nKeep this section.\n\n" in merged
    assert "```markdown\n## Scope\nThis is literal text.\n```" in merged
    assert "## Scope\nUpdated Scope." in merged
    assert merged.count("## Outcome\n") == 1


def test_refinement_section_merge_rejects_ambiguous_duplicate_headings() -> None:
    with pytest.raises(PbiRefinementMutationError, match="Duplicate managed") as error:
        merge_refinement_sections("## Outcome\nOne\n\n## Outcome\nTwo\n", _sections())

    assert error.value.code == "duplicate_section_heading"
    assert error.value.status_code == 409


def test_refinement_section_merge_preserves_html_comments_with_heading_text() -> None:
    body = "<!--\n## Outcome\nCommented text stays intact.\n-->\n"

    merged = merge_refinement_sections(body, _sections())

    assert merged.startswith(body)
    assert "## Outcome\nUpdated Outcome." in merged


def test_refinement_section_merge_ignores_heading_after_comment_closer() -> None:
    body = "<!--\ncomment\n-->## Outcome\nPreserve this line\n## Notes\nNotes.\n"

    merged = merge_refinement_sections(body, _sections())

    assert merged.startswith(body)


def test_refinement_section_merge_preserves_headings_inside_raw_html_blocks() -> None:
    body = (
        "<div>\n## Outcome\nRaw HTML text stays intact.\n</div>\n\n"
        "## Outcome\nOld outcome.\n"
    )

    merged = merge_refinement_sections(body, _sections())

    assert merged.startswith(
        "<div>\n## Outcome\nRaw HTML text stays intact.\n</div>\n\n"
        "## Outcome\nUpdated Outcome."
    )


def test_refinement_section_merge_ignores_comment_marker_inside_inline_code() -> None:
    body = (
        "## Outcome\nOld outcome.\n\n"
        "## Notes\nLiteral `<!--` marker.\n\n"
        "## Scope\nOld scope.\n"
    )

    merged = merge_refinement_sections(body, _sections())

    assert "## Notes\nLiteral `<!--` marker.\n\n" in merged
    assert "## Scope\nUpdated Scope." in merged


def test_refinement_request_validates_sections_and_classification_labels() -> None:
    _request().validate()

    with pytest.raises(
        PbiRefinementMutationError, match="required refinement sections"
    ):
        _request(sections={"Outcome": "Only one section"}).validate()
    with pytest.raises(PbiRefinementMutationError, match="live effort label"):
        _request(effort_label="Effort 4 - Strange").validate()
    with pytest.raises(PbiRefinementMutationError, match="live effort label"):
        _request(effort_label="Effort 13 - Candidate").validate()
    with pytest.raises(PbiRefinementMutationError, match="unique"):
        _request(standard_labels=("enhancement", "Enhancement")).validate()


def test_refinement_request_rejects_managed_heading_inside_section_content() -> None:
    sections = _sections()
    sections["Scope"] = "Text\n## Outcome\nAmbiguous section content"

    with pytest.raises(PbiRefinementMutationError, match="managed level-two heading"):
        _request(sections=sections).validate()
