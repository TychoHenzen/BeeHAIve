from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

REFINEMENT_SECTION_ORDER = (
    "Outcome",
    "Scope",
    "Implementation notes",
    "Acceptance criteria",
    "Verification",
)
MAX_REFINEMENT_BODY_LENGTH = 65_536
MAX_REFINEMENT_LABELS = 20
_MANAGED_HEADING = re.compile(r"^ {0,3}##[ \t]+(.+?)[ \t]*#*[ \t]*$")
_ANY_LEVEL_TWO_HEADING = re.compile(r"^ {0,3}##(?:[ \t]+|$)")
_FENCE_START = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_RAW_HTML_TAG_START = re.compile(
    r"^ {0,3}<(script|pre|style|textarea)(?:[ \t/>]|$)", re.IGNORECASE
)
_HTML_BLOCK_TAG_START = re.compile(
    r"^ {0,3}</?(?:address|article|aside|base|basefont|blockquote|body|caption|"
    r"center|col|colgroup|dd|details|dialog|dir|div|dl|dt|fieldset|figcaption|"
    r"figure|footer|form|frame|frameset|h[1-6]|head|header|hr|html|iframe|legend|"
    r"li|link|main|menu|menuitem|meta|nav|noframes|ol|optgroup|option|p|param|"
    r"search|section|summary|table|tbody|td|tfoot|th|thead|title|tr|track|ul)"
    r"(?:[ \t/>]|$)",
    re.IGNORECASE,
)
_HTML_TAG_LINE = re.compile(
    r"^ {0,3}</?[A-Za-z][A-Za-z0-9-]*(?:[ \t]+[^<>]*)?/?>[ \t]*$"
)
_HTML_BLOCK_ENDS = (
    (re.compile(r"^ {0,3}<!\[CDATA\["), r"\]\]>"),
    (re.compile(r"^ {0,3}<![A-Z]"), r">"),
    (re.compile(r"^ {0,3}<\?"), r"\?>"),
)
_PRIORITY_SCALE_LABEL = re.compile(r"Prio [1-6] - .+", re.IGNORECASE)
_EFFORT_SCALE_LABEL = re.compile(r"Effort (?:1|2|3|5|8) - .+", re.IGNORECASE)
_EPIC_SCALE_LABEL = "Effort 13 - Epic"


def is_pbi_refinement_scale_label(name: str) -> bool:
    return bool(
        _PRIORITY_SCALE_LABEL.fullmatch(name)
        or _EFFORT_SCALE_LABEL.fullmatch(name)
        or name.casefold() == _EPIC_SCALE_LABEL.casefold()
    )


class PbiRefinementMutationError(ValueError):
    """A bounded request or live-state conflict for a PBI refinement write."""

    def __init__(self, message: str, *, code: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class PbiRefinementUpdateRequest:
    project_id: str
    repository: str
    pbi_number: int
    sections: Mapping[str, str]
    priority_label: str
    effort_label: str
    standard_labels: tuple[str, ...] = ()

    def validate(self) -> None:
        if not self.project_id or self.project_id != self.project_id.strip():
            raise PbiRefinementMutationError(
                "Project id is required", code="invalid_project"
            )
        if (
            not self.repository
            or self.repository != self.repository.strip()
            or self.repository.count("/") != 1
        ):
            raise PbiRefinementMutationError(
                "Repository must use owner/name format", code="invalid_repository"
            )
        if (
            type(self.pbi_number) is not int
            or not 1 <= self.pbi_number <= 2_147_483_647
        ):
            raise PbiRefinementMutationError(
                "PBI number is outside the supported range", code="invalid_pbi_number"
            )
        if set(self.sections) != set(REFINEMENT_SECTION_ORDER):
            raise PbiRefinementMutationError(
                "Exactly the five required refinement sections are required",
                code="invalid_sections",
            )
        total_section_length = 0
        for name in REFINEMENT_SECTION_ORDER:
            value = self.sections[name]
            if not value.strip():
                raise PbiRefinementMutationError(
                    f"{name} section is required", code="invalid_sections"
                )
            total_section_length += len(value)
            if _contains_managed_heading(value):
                raise PbiRefinementMutationError(
                    "Section content cannot contain a managed level-two heading",
                    code="invalid_sections",
                )
        if total_section_length > MAX_REFINEMENT_BODY_LENGTH:
            raise PbiRefinementMutationError(
                "Refinement sections exceed the body limit", code="body_too_large"
            )
        if not _PRIORITY_SCALE_LABEL.fullmatch(self.priority_label):
            raise PbiRefinementMutationError(
                "A live priority label is required", code="invalid_priority_label"
            )
        if not (
            _EFFORT_SCALE_LABEL.fullmatch(self.effort_label)
            or self.effort_label == _EPIC_SCALE_LABEL
        ):
            raise PbiRefinementMutationError(
                "A live effort label is required", code="invalid_effort_label"
            )
        labels = (
            self.priority_label,
            self.effort_label,
            *self.standard_labels,
        )
        if len(self.standard_labels) > MAX_REFINEMENT_LABELS:
            raise PbiRefinementMutationError(
                "At most 20 standard labels may be supplied", code="invalid_labels"
            )
        normalized: set[str] = set()
        for label in labels:
            if not label or label != label.strip() or len(label) > 100:
                raise PbiRefinementMutationError(
                    "Labels must contain 1 to 100 non-whitespace characters",
                    code="invalid_labels",
                )
            key = label.casefold()
            if key in normalized:
                raise PbiRefinementMutationError(
                    "Requested labels must be unique", code="invalid_labels"
                )
            normalized.add(key)
        if any(is_pbi_refinement_scale_label(label) for label in self.standard_labels):
            raise PbiRefinementMutationError(
                "Standard labels cannot replace priority or effort labels",
                code="invalid_labels",
            )


@dataclass(frozen=True, slots=True)
class PbiRefinementUpdateResult:
    status: Literal["complete", "partial"]
    issue_number: int
    issue_url: str
    labels: tuple[str, ...]
    project_item_id: str
    project_status: str
    linked_sub_issues: tuple[Mapping[str, object], ...]
    completed_steps: tuple[str, ...]
    pending_step: str | None = None
    failure_code: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "issue": {"number": self.issue_number, "url": self.issue_url},
            "labels": list(self.labels),
            "project": {
                "item_id": self.project_item_id,
                "status": self.project_status,
            },
            "linked_sub_issues": [dict(issue) for issue in self.linked_sub_issues],
            "completed_steps": list(self.completed_steps),
            "pending_step": self.pending_step,
            "failure_code": self.failure_code,
        }


@dataclass(frozen=True, slots=True)
class PbiRefinementTarget:
    project_node_id: str
    status_field_id: str
    backlog_option_id: str
    backlog_status: str
    todo_option_id: str
    todo_status: str


class PbiRefinementMutationProvider(Protocol):
    def apply_pbi_refinement(
        self, request: PbiRefinementUpdateRequest
    ) -> PbiRefinementUpdateResult: ...


def merge_refinement_sections(body: str, sections: Mapping[str, str]) -> str:
    if set(sections) != set(REFINEMENT_SECTION_ORDER):
        raise PbiRefinementMutationError(
            "Exactly the five required refinement sections are required",
            code="invalid_sections",
        )
    newline = "\r\n" if "\r\n" in body else "\n"
    lines = body.splitlines(keepends=True)
    headings = _markdown_headings(lines)
    managed = [
        (index, title) for index, title in headings if title in REFINEMENT_SECTION_ORDER
    ]
    counts: dict[str, int] = {}
    for _, title in managed:
        counts[title] = counts.get(title, 0) + 1
    if any(count > 1 for count in counts.values()):
        raise PbiRefinementMutationError(
            "Duplicate managed refinement headings are ambiguous",
            code="duplicate_section_heading",
            status_code=409,
        )

    replacements: dict[int, tuple[int, list[str]]] = {}
    present = {title for _, title in managed}
    for index, title in managed:
        end = next(
            (heading_index for heading_index, _ in headings if heading_index > index),
            len(lines),
        )
        old_section = lines[index + 1 : end]
        trailing_start = len(old_section)
        while trailing_start and not old_section[trailing_start - 1].strip():
            trailing_start -= 1
        rendered = _render_section(
            lines[index],
            sections[title],
            newline,
            old_section[trailing_start:],
        )
        replacements[index] = (end, rendered)

    merged: list[str] = []
    index = 0
    while index < len(lines):
        replacement = replacements.get(index)
        if replacement is None:
            merged.append(lines[index])
            index += 1
        else:
            end, rendered = replacement
            merged.extend(rendered)
            index = end

    missing = [name for name in REFINEMENT_SECTION_ORDER if name not in present]
    if missing:
        current = "".join(merged)
        if current and not current.endswith(("\n", "\r")):
            merged.append(newline)
            current += newline
        if current and not current.endswith(newline * 2):
            merged.append(newline)
        for offset, name in enumerate(missing):
            merged.extend(
                _render_section(f"## {name}{newline}", sections[name], newline, [])
            )
            if offset < len(missing) - 1:
                merged.append(newline)

    result = "".join(merged)
    if len(result) > MAX_REFINEMENT_BODY_LENGTH:
        raise PbiRefinementMutationError(
            "Updated issue body exceeds the body limit", code="body_too_large"
        )
    return result


def _render_section(
    heading: str,
    text: str,
    newline: str,
    trailing_lines: list[str],
) -> list[str]:
    rendered_heading = heading if heading.endswith(("\n", "\r")) else heading + newline
    normalized_text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    rendered_text = [line + newline for line in normalized_text.split("\n")]
    return [rendered_heading, *rendered_text, *trailing_lines]


def _markdown_headings(lines: list[str]) -> list[tuple[int, str]]:
    headings: list[tuple[int, str]] = []
    fence_character: str | None = None
    fence_length = 0
    inside_html_comment = False
    html_block_end: tuple[str, bool] | None = None
    for index, line in enumerate(lines):
        value = line.rstrip("\r\n")
        if fence_character is not None:
            if re.fullmatch(
                rf" {{0,3}}{re.escape(fence_character)}{{{fence_length},}}[ \t]*",
                value,
            ):
                fence_character = None
                fence_length = 0
            continue
        if html_block_end is not None:
            end_pattern, ends_at_blank = html_block_end
            if (ends_at_blank and not value.strip()) or (
                end_pattern and re.search(end_pattern, value, re.IGNORECASE)
            ):
                html_block_end = None
            continue
        was_inside_html_comment = inside_html_comment
        was_heading_before_comment_stripping = (
            _ANY_LEVEL_TWO_HEADING.match(value) is not None
        )
        value, inside_html_comment = _strip_html_comments(value, inside_html_comment)
        if was_inside_html_comment or (
            not was_heading_before_comment_stripping
            and _ANY_LEVEL_TWO_HEADING.match(value)
        ):
            continue
        fence = _FENCE_START.match(value)
        if fence is not None:
            delimiter = fence.group(1)
            fence_character = delimiter[0]
            fence_length = len(delimiter)
            continue
        raw_tag = _RAW_HTML_TAG_START.match(value)
        if raw_tag is not None:
            end_pattern = rf"</\s*{re.escape(raw_tag.group(1))}\s*>"
            if not re.search(end_pattern, value, re.IGNORECASE):
                html_block_end = (end_pattern, False)
            continue
        block_end = next(
            (end for start, end in _HTML_BLOCK_ENDS if start.match(value)), None
        )
        if block_end is not None:
            if not re.search(block_end, value, re.IGNORECASE):
                html_block_end = (block_end, False)
            continue
        if _HTML_BLOCK_TAG_START.match(value) or _HTML_TAG_LINE.fullmatch(value):
            html_block_end = ("", True)
            continue
        if not _ANY_LEVEL_TWO_HEADING.match(value):
            continue
        heading = _MANAGED_HEADING.match(value)
        title = heading.group(1).strip().rstrip("# ").strip() if heading else ""
        headings.append((index, title))
    return headings


def _strip_html_comments(value: str, inside_comment: bool) -> tuple[str, bool]:
    visible: list[str] = []
    code_spans = _inline_code_spans(value)
    code_span_index = 0
    position = 0
    while position < len(value):
        if not inside_comment:
            while (
                code_span_index < len(code_spans)
                and code_spans[code_span_index][1] <= position
            ):
                code_span_index += 1
            if (
                code_span_index < len(code_spans)
                and code_spans[code_span_index][0] <= position
            ):
                end = code_spans[code_span_index][1]
                visible.append(value[position:end])
                position = end
                continue
        marker = "-->" if inside_comment else "<!--"
        marker_index = value.find(marker, position)
        if (
            not inside_comment
            and code_span_index < len(code_spans)
            and code_spans[code_span_index][0]
            < marker_index
            < code_spans[code_span_index][1]
        ):
            end = code_spans[code_span_index][1]
            visible.append(value[position:end])
            position = end
            continue
        if marker_index == -1:
            visible.append(
                " " * (len(value) - position) if inside_comment else value[position:]
            )
            break
        if inside_comment:
            visible.append(" " * (marker_index + len(marker) - position))
            position = marker_index + len(marker)
            inside_comment = False
        else:
            visible.append(value[position:marker_index])
            position = marker_index + len(marker)
            inside_comment = True
    return "".join(visible), inside_comment


def _inline_code_spans(value: str) -> list[tuple[int, int]]:
    runs: list[tuple[int, int, int, bool]] = []
    position = 0
    backslash_count = 0
    while position < len(value):
        if value[position] == "`":
            start = position
            while position < len(value) and value[position] == "`":
                position += 1
            runs.append((start, position, position - start, backslash_count % 2 == 1))
            backslash_count = 0
        else:
            backslash_count = backslash_count + 1 if value[position] == "\\" else 0
            position += 1

    next_run_by_length: dict[int, int] = {}
    closing_runs: list[int | None] = [None] * len(runs)
    for index in range(len(runs) - 1, -1, -1):
        _, _, length, escaped = runs[index]
        if not escaped:
            closing_runs[index] = next_run_by_length.get(length)
            next_run_by_length[length] = index

    spans: list[tuple[int, int]] = []
    index = 0
    while index < len(runs):
        start = runs[index][0]
        closing_index = closing_runs[index]
        if closing_index is None:
            index += 1
            continue
        spans.append((start, runs[closing_index][1]))
        index = closing_index + 1
    return spans


def _contains_managed_heading(value: str) -> bool:
    return any(
        title in REFINEMENT_SECTION_ORDER
        for _, title in _markdown_headings(value.splitlines(keepends=True))
    )
