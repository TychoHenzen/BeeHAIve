from __future__ import annotations

import re

REFINEMENT_SECTION_ORDER = (
    "Outcome",
    "Scope",
    "Implementation notes",
    "Acceptance criteria",
    "Verification",
)

MAX_REFINEMENT_BODY_LENGTH = 65_536

MAX_REFINEMENT_LABELS = 20

_MANAGED_HEADING = re.compile(r"^ {0,3}##[ \t]+(.+?)(?:[ \t]+#+)?[ \t]*$")

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

__all__ = [
    "MAX_REFINEMENT_BODY_LENGTH",
    "MAX_REFINEMENT_LABELS",
    "REFINEMENT_SECTION_ORDER",
    "_ANY_LEVEL_TWO_HEADING",
    "_EFFORT_SCALE_LABEL",
    "_EPIC_SCALE_LABEL",
    "_FENCE_START",
    "_HTML_BLOCK_ENDS",
    "_HTML_BLOCK_TAG_START",
    "_HTML_TAG_LINE",
    "_MANAGED_HEADING",
    "_PRIORITY_SCALE_LABEL",
    "_RAW_HTML_TAG_START",
]
