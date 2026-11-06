"""tests_bugfix/test_bugfix_340_extraction.py — AGNES-1 (round 340).

Four defects in what `extract_block` / `_extract_python_block_fallback` /
`_parse_via_regex` answer. All four return a plausible answer, so nothing
downstream notices, and each is reproduced on `4526a0a`:

* **T79** — a declaration written inside a comment or a string is taken for a
  real one: `int proto(int a);` inside a `/* … */` block comment was
  extracted for `proto`. A declaration inside a TS string is likewise not a
  definition, and a real declaration of the same name later in the file is
  still found.
* **T89** — the Python fallback's signature scan splits a line on `#` even
  when it is inside a string, so `def f(x="a#b"):` never found its `:` and
  ran on into `def after():`. Delimiters inside strings must not be counted
  either, so `def f(x="(", y="#"):` stops at its own body.
* **T93** — an unterminated JS regex literal (`const r = /{`) is read to the
  end of its line only, so its `{` no longer inflates the brace count and the
  block no longer swallows the function after the target. `/{/;` is
  terminated and stays a regex.
* **T96** — a reserved word after a declaration keyword (`def return`) is not
  a symbol. A non-keyword name absent from the source (`def new_helper`) is
  still accepted, because a request may name a new symbol.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.block_extractor import extract_block, _extract_python_block_fallback  # noqa: E402
from tools.prompt_parser import _parse_via_regex  # noqa: E402


# ── T79: a declaration inside a comment or a string is not a definition ──────

def test_declaration_inside_a_block_comment_is_not_a_definition():
    src = "/*\nint proto(int a);\n*/\nint other(void) { return 0; }\n"
    assert extract_block(src, "proto", ".c") == ""


def test_declaration_inside_a_string_is_not_a_definition():
    src = 'const s = "function foo() { return 1; }";\n'
    assert extract_block(src, "foo", ".ts") == ""


def test_a_real_declaration_after_the_commented_one_is_still_found():
    src = (
        "/*\nint proto(int a);\n*/\n"
        "int proto(void) { return 0; }\n"
    )
    blk = extract_block(src, "proto", ".c")
    assert "int proto(void) { return 0; }" in blk
    assert "int proto(int a);" not in blk


def test_a_real_declaration_after_the_string_one_is_still_found():
    src = 'const s = "function foo() { return 1; }";\nfunction foo() { return 2; }\n'
    blk = extract_block(src, "foo", ".ts")
    assert "function foo() { return 2; }" in blk
    assert "return 1;" not in blk


# ── T89: `#` and delimiters inside a string in the Python fallback ──────────

def test_python_fallback_hash_inside_a_default_stops_before_the_next_def():
    src = 'def f(x="a#b"):\n    return x\n\ndef after():\n    return 2\n'
    blk = _extract_python_block_fallback(src, "f")
    assert "def after" not in blk
    assert blk.rstrip().endswith("return x")


def test_python_fallback_delimiters_inside_a_default_stop_at_its_own_body():
    src = 'def f(x="(", y="#"):\n    return x\n\ndef after():\n    return 2\n'
    blk = _extract_python_block_fallback(src, "f")
    assert "def after" not in blk
    assert blk.rstrip().endswith("return x")


# ── T93: an unterminated JS regex literal is only its own line ───────────────

def test_unterminated_js_regex_does_not_swallow_the_next_function():
    src = (
        "function f() {\n"
        "  const r = /{\n"
        "  return 1;\n"
        "}\n"
        "function after() { return 2; }\n"
    )
    blk = extract_block(src, "f", ".js")
    assert "function after" not in blk
    assert blk.rstrip().endswith("}")


def test_a_terminated_js_regex_is_still_a_regex():
    src = (
        "function f() {\n"
        "  const r = /{/;\n"
        "  return 1;\n"
        "}\n"
        "function after() { return 2; }\n"
    )
    blk = extract_block(src, "f", ".js")
    assert "/{/;" in blk
    assert "function after" not in blk


# ── T96: a reserved word after a declaration keyword is not a symbol ────────

def test_a_python_reserved_word_after_def_is_not_a_symbol():
    parsed = _parse_via_regex(
        "show the def return function in a.py",
        source="def handle_request():\n    return 1\n",
    )
    assert parsed is not None
    assert parsed.target_name != "return"


def test_a_non_keyword_name_absent_from_the_source_is_still_accepted():
    # A request may name a symbol that does not exist yet, so an unknown,
    # non-keyword name after `def` is still the target.
    parsed = _parse_via_regex("show def new_helper in a.py")
    assert parsed is not None
    assert parsed.target_name == "new_helper"


# ── A stray quote does not hide the rest of the file ─────────────────────────

AFTER_JS = "function after() { return 9; }\n"


@pytest.mark.parametrize("head, ext", [
    ("const a = <p>don't stop</p>;\n", ".tsx"),
    ('const a = <p>5" wide</p>;\n', ".tsx"),
    ("if (x) /'/.test(y);\n", ".js"),
    ("// don't\n/* can't */\n", ".ts"),
])
def test_a_stray_quote_does_not_hide_the_definitions_below_it(head, ext):
    assert "return 9" in extract_block(head + AFTER_JS, "after", ext)


def test_a_backtick_string_still_spans_lines():
    src = "const s = `\nfunction foo() { return 1; }\n`;\nfunction bar() {}\n"
    assert extract_block(src, "foo", ".ts") == ""


def test_a_function_inside_a_c_macro_is_still_found():
    src = "#define MK(name) \\\n  static int name(void) { return 9; }\n"
    assert "return 9" in extract_block(src, "name", ".c")


# ── A word reserved elsewhere is a name when the source defines it ───────────

@pytest.mark.parametrize("word", ["function", "default", "new", "static"])
def test_a_class_named_with_another_languages_reserved_word_is_found(word):
    source = f"class {word}:\n    pass\n"
    parsed = _parse_via_regex(f"show the class {word} in a.py", source=source)
    assert parsed.target_name == word


@pytest.mark.parametrize("word", ["range", "type", "select", "map", "match"])
def test_a_def_named_with_a_word_reserved_only_in_go_is_found(word):
    source = f"def {word}():\n    return 1\n"
    parsed = _parse_via_regex(f"show the def {word} function in a.py", source=source)
    assert parsed.target_name == word
