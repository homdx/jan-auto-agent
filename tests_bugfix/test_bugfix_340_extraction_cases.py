"""tests_bugfix/test_bugfix_340_extraction_cases.py — AGNES-1 (340-agnes-1-code-extraction-hands-the-agent-the-wrong-code).

Four extraction defects, each reproduced on 4526a0a: every one returned a
plausible-looking answer, so nothing downstream noticed.

1. AUTO-T79 — a declaration that stands in a comment or a string literal
   (`/* int proto(int a); */`, `const s = "function foo() { … }"`) was
   answered with that mention as if it were the definition.
2. AUTO-T89 — the Python fallback split the signature line on the first `#`,
   so a `#` inside a default argument (`def f(x="a#b"):`) ended the scan and
   swallowed the next top-level `def`; delimiters inside strings were also
   counted, so `def f(x="(", y="#"):` never found its own `:`.
3. AUTO-T93 — a JS regex literal that never closes on its line (`const r = /{`)
   made the brace count read that `{` as a body brace, so the target block ran
   on into the next function.
4. AUTO-T96 — a reserved word after `def`/`func`/`function`/`class`
   ("show the def return function in a.py") was parsed as the target symbol
   `return` instead of as prose.

Each test below failed on 4526a0a; the pins at the end of the file record
what must NOT change: a real declaration later in the file is still found,
a terminated regex is unchanged, and a non-keyword name that is absent from
the source (`def new_helper`) is still accepted.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.block_extractor import extract_block, _extract_python_block_fallback  # noqa: E402
from tools.prompt_parser import _parse_via_regex  # noqa: E402


# ── AUTO-T79: a declaration in a comment or a string is a mention, not a definition ──

def test_c_declaration_inside_a_block_comment_is_not_found():
    src = "/*\nint proto(int a);\n*/\nint other(void) { return 0; }\n"
    assert extract_block(src, "proto", ".c") == ""
    # The file still answers for the symbol it really defines.
    assert "return 0;" in extract_block(src, "other", ".c")


def test_a_commented_out_braced_function_is_not_a_definition():
    src = "/*\nfunction foo() {\n  return 1;\n}\n*/\nfunction bar() { return 2; }\n"
    assert extract_block(src, "foo", ".js") == ""


def test_string_quoted_function_declaration_is_not_a_definition():
    src = 'const s = "function foo() { return 1; }";\n'
    assert extract_block(src, "foo", ".ts") == ""
    assert extract_block(src, "foo", ".js") == ""


def test_a_multiline_string_quoted_declaration_is_not_a_definition():
    # The regex head is anchored at a line start, and a template literal puts its
    # quoted code on its own line: the name stands inside the literal's span.
    src = "const s = `\nfunction foo() { return 1; }\n`;\nfunction bar() { return 2; }\n"
    assert extract_block(src, "foo", ".js") == ""
    assert extract_block(src, "bar", ".js") != ""


def test_a_real_declaration_later_in_the_file_is_still_found():
    src = "/*\nint proto(int a);\n*/\nint proto(int a);\n"
    assert extract_block(src, "proto", ".c") == "int proto(int a);"


def test_a_real_declaration_after_a_commented_out_one_is_still_the_block():
    src = "/* int proto(int a); */\nint proto(int a) { }\n"
    assert extract_block(src, "proto", ".c") == "int proto(int a) { }"


# ── AUTO-T89: `#` inside a default argument, and delimiters inside strings ──

def test_a_hash_inside_a_default_argument_does_not_end_the_signature():
    src = 'def f(x="a#b"):\n    return x\n\ndef after():\n    return 2\n'
    blk = _extract_python_block_fallback(src, "f")
    assert "def after" not in blk
    assert "return x" in blk


def test_delimiters_inside_a_string_are_not_counted_in_the_signature():
    src = 'def f(x="(", y="#"):\n    return x\n\ndef after():\n    return 2\n'
    blk = _extract_python_block_fallback(src, "f")
    assert "def after" not in blk
    assert "return x" in blk


def test_a_plain_comment_after_the_signature_still_ends_the_scan():
    src = "def f():  # the # comment's brace } cannot close the signature\n    return 1\n"
    blk = _extract_python_block_fallback(src, "f")
    assert "return 1" in blk


def test_a_escaped_quote_inside_a_string_is_still_part_of_the_string():
    src = 'def f(x="a\\"#b"):\n    return x\n\ndef after():\n    return 2\n'
    blk = _extract_python_block_fallback(src, "f")
    assert "def after" not in blk
    assert "return x" in blk


# ── AUTO-T93: a JS regex that never closes on its line holds no braces ──

def test_an_unterminated_js_regex_does_not_swallow_the_next_function():
    src = (
        "function f() {\n"
        "  const r = /{\n"
        "  return 1;\n"
        "}\n"
        "function after() { return 2; }\n"
    )
    blk = extract_block(src, "f", ".js")
    assert "after" not in blk
    assert "return 1;" in blk
    assert blk.rstrip().endswith("}")


def test_a_terminated_js_regex_with_a_brace_is_not_an_unterminated_one():
    src = (
        "function g() {\n"
        "  const r = /{/;\n"
        "  return 1;\n"
        "}\n"
        "function after() { return 2; }\n"
    )
    blk = extract_block(src, "g", ".js")
    assert "after" not in blk
    assert "/{/;" in blk


def test_a_jsx_closing_tag_is_still_not_a_regex():
    # `/>` is the JSX tag's shape: the `)` and `;` after it are code the scan keeps.
    src = (
        "export const Input = React.forwardRef((props, ref) => <input ref={ref} />);\n"
        "function after() { return 2; }\n"
    )
    blk = extract_block(src, "Input", ".tsx")
    assert "after" not in blk
    assert blk.rstrip().endswith(";")


# ── AUTO-T96: a reserved word after def/func/function/class is prose, not a symbol ──

def test_a_reserved_word_after_def_is_not_the_target():
    res = _parse_via_regex(
        "show the def return function in a.py",
        source="def handle_request():\n    return 1\n",
    )
    assert res is not None
    assert res.target_name != "return"


def test_a_reserved_word_after_function_is_not_the_target():
    res = _parse_via_regex("show the function return in a.py", source="def f():\n    return 1\n")
    assert res is not None
    assert res.target_name != "return"


def test_a_reserved_word_after_class_is_not_the_target():
    res = _parse_via_regex("show the class while in a.py", source="class A:\n    pass\n")
    assert res is not None
    assert res.target_name != "while"


def test_a_reserved_word_is_rejected_even_without_a_source():
    res = _parse_via_regex("show the def return function in a.py")
    assert res is not None
    assert res.target_name != "return"


def test_a_non_keyword_name_absent_from_the_source_is_still_accepted():
    # A request may name a symbol that does not exist yet (a new function to add).
    res = _parse_via_regex(
        "show the def new_helper function in a.py",
        source="def handle_request():\n    return 1\n",
    )
    assert res is not None
    assert res.target_name == "new_helper"


def test_a_capitalized_keyword_spelling_is_still_a_legal_identifier():
    # `Return` is not a reserved word in Python, JS or Go: only the lowercase form is.
    res = _parse_via_regex(
        "show the def Return function in a.py",
        source="def handle_request():\n    return 1\n",
    )
    assert res is not None
    assert res.target_name == "Return"
