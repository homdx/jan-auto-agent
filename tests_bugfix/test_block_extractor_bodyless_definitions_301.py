"""tests_bugfix/test_block_extractor_bodyless_definitions_301.py

Bug: `extract_block` returned "" for a definition whose body has no
`{ … }`, so Gate 1 Stage A answered "symbol not found" — a hallucination —
for a function that exists. The arrow pattern matched
`const double = (x) => x * 2;`, but `_find_definition_open_brace` found no
`{` and threw the match away; `const f = function () {}`,
`module.exports = function m() {}`, `var F = func() {}` and `fun ff() = 1`
matched no pattern at all.

Field report: HARDEN-4 D2, reproduced on the six shapes below (plus
`function triple() {}`, which was already found) before the fix.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.block_extractor import extract_block  # noqa: E402

TABLE = [
    ("const double = (x) => x * 2;\n", "double", ".ts"),
    ("const clamp = (a, b) =>\n  a + b;\n", "clamp", ".ts"),
    ("const f = function () {}\n", "f", ".js"),
    ("module.exports = function m() {}\n", "m", ".js"),
    ("var F = func() {}\n", "F", ".go"),
    ("fun ff() = 1\n", "ff", ".kt"),
]


def test_every_bodyless_form_is_found():
    for source, name, ext in TABLE:
        assert extract_block(source, name, ext), f"{name!r} ({ext}) not found in {source!r}"


def test_expression_body_is_returned_whole():
    assert extract_block("const double = (x) => x * 2;\n", "double", ".ts").strip() == \
        "const double = (x) => x * 2;"


def test_continuation_line_belongs_to_the_definition():
    blk = extract_block("const clamp = (a, b) =>\n  a + b;\n", "clamp", ".ts")
    assert blk == "const clamp = (a, b) =>\n  a + b;"


def test_kotlin_expression_body_stops_at_end_of_line():
    blk = extract_block("fun ff() = 1\nfun gg() = 2\n", "ff", ".kt")
    assert blk.strip() == "fun ff() = 1"


def test_unknown_name_still_returns_empty():
    assert extract_block("const double = (x) => x * 2;\n", "doesNotExist", ".ts") == ""


def test_a_call_site_is_still_not_a_definition():
    # The AUTO-BUG guard this must not reopen: only a declaration head answers.
    assert extract_block("function main() {\n    helper(1, 2);\n}\n", "helper", ".js") == ""
