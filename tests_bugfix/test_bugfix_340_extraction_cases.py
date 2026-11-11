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


# ── 340 follow-up: the new masks and the signature scan must keep their own state ──

def test_js_template_interpolation_is_code_but_the_template_text_is_not() -> None:
    """`${ … }` holds executable JS (an IIFE with `function foo`); the text around it is a string."""
    source = ("const text = `hello ${(() => {\n"
              "function foo() { return 1; }\n"
              "return foo();\n"
              "})()}`;\n")
    assert extract_block(source, "foo", ".js") == "function foo() { return 1; }"
    text_only = "const t = `function bar() { return 1; } ${x}`;\nfunction real() { return 2; }\n"
    assert extract_block(text_only, "bar", ".js") == ""
    assert extract_block(text_only, "real", ".js").strip() == "function real() { return 2; }"


def test_js_nested_templates_and_object_literals_inside_an_interpolation() -> None:
    source = ("const t = `a ${ {k: 1}.k } b ${`c ${(() => {\n"
              "  function inner() { return 2; }\n"
              "  return inner(); })()}`}`;\n"
              "function after() { return 3; }\n")
    assert extract_block(source, "inner", ".js").strip() == "function inner() { return 2; }"
    assert extract_block(source, "after", ".js").strip() == "function after() { return 3; }"


def test_python_fallback_keeps_a_multiline_triple_quote_open_across_lines() -> None:
    """A syntax error elsewhere forces the fallback; `f` must still end at its own `:`, before `after`."""
    for inner in ("first\nsecond\nlast", "first\n(\nlast", "a\n):\nb"):
        source = (f'def f(x="""{inner}"""):\n    return x\n\ndef after():\n    return 2\n\nbroken = (\n')
        block = extract_block(source, "f", ".py")
        assert "return x" in block, inner
        assert "def after" not in block and "broken" not in block, inner


def test_python_fallback_ends_a_lone_quote_at_its_line() -> None:
    """Only a triple quote spans lines: a stray `'` must not swallow the next line's colon."""
    source = "def f(x=1):  # it's\n    return x\n\ndef after():\n    return 2\n\nbroken = (\n"
    block = extract_block(source, "f", ".py")
    assert "return x" in block and "def after" not in block
    continued = 'def g(x="a\\\n(b"):\n    return x\n\ndef after():\n    return 2\n\nbroken = (\n'
    block = extract_block(continued, "g", ".py")
    assert "return x" in block and "def after" not in block


def test_java_text_block_holds_no_declaration() -> None:
    source = ('class Demo {\n    String text = """\nvoid fake() {}\n""";\n    void real() {}\n}\n')
    assert extract_block(source, "fake", ".java") == ""
    assert extract_block(source, "real", ".java").strip() == "void real() {}"


def test_a_text_block_with_an_unbalanced_brace_does_not_break_the_next_method() -> None:
    source = ('class Demo {\n    String t = """\n  { not a body \\"""still text\n""";\n'
              '    void real() { return; }\n    void other() {}\n}\n')
    assert extract_block(source, "real", ".java").strip() == "void real() { return; }"


def test_a_definition_inside_a_string_or_comment_is_not_what_the_prompt_names() -> None:
    """`def return()` written in a docstring or a comment names nothing: `return` is prose."""
    for source in ('DOC = """\ndef return():\n    pass\n"""\n\ndef handle():\n    return 1\n',
                   "# def return\ndef handle():\n    return 1\n",
                   "'''\ndef return(): pass\n'''\n"):
        parsed = _parse_via_regex("show the def return function in a.py", source)
        assert parsed is not None and parsed.target_name != "return", source
    real = _parse_via_regex("show def handle in a.py", "def handle():\n    return 1\n")
    assert real is not None and real.target_name == "handle"


def test_a_slash_after_a_postfix_operator_is_a_division_not_a_regex() -> None:
    """`x++ / 2` ends an operand, so the `/` divides; read as an unclosed regex it ate the next function."""
    for operator in ("++", "--"):
        target = f"function target(x) {{ return x{operator} / 2; }}"
        source = target + "\nfunction neighbor() { return 42; }\n"
        assert extract_block(source, "target", ".js") == target
    # the unclosed regex of T93 is still read to the end of its line, and a spaced prefix form is no postfix
    unclosed = "function target() { const r = /{;\nreturn 1;\n}\nfunction neighbor() { return 42; }\n"
    assert extract_block(unclosed, "target", ".js") == "function target() { const r = /{;\nreturn 1;\n}"
    regex_after_plus = "function target(s) { return 1 + /}/.test(s); }\nfunction neighbor() { return 42; }\n"
    assert extract_block(regex_after_plus, "target", ".js") == "function target(s) { return 1 + /}/.test(s); }"


def test_a_cpp_raw_string_is_one_literal_up_to_its_own_delimiter() -> None:
    """`R"(…)"` spans lines and may hold `/*`, a quote or a whole fake function."""
    cases = (
        'const char *s = R"(text\n/* ordinary string contents\n)";\nint target() { return 1; }\n',
        'const char *s = R"x(a )" /* b\n)x";\nint target() { return 1; }\nint other() { return 2; }\n',
        'const char *s = u8R"(int target() { return 9; })";\nint target() { return 1; }\n',
    )
    for source in cases:
        assert extract_block(source, "target", ".cpp").strip() == "int target() { return 1; }", source
    # a real comment still hides a prototype, and an identifier ending in R before a quote is no raw prefix
    assert extract_block("/*\nint target();\n*/\nint target() { return 1; }\n", "target", ".cpp").strip() \
        == "int target() { return 1; }"
    assert extract_block('#define FOR"(" \nint target() { return 1; }\n', "target", ".cpp").strip() \
        == "int target() { return 1; }"
    # inside a body the raw string's braces are not the body's
    body = 'int target() { const char *s = R"({ not a brace)"; return 1; }\nint after() { return 2; }\n'
    assert extract_block(body, "target", ".cpp").strip() == 'int target() { const char *s = R"({ not a brace)"; return 1; }'


def test_a_slash_after_a_ts_non_null_bang_or_an_object_literal_is_a_division() -> None:
    cases = (
        (".ts", "function target(x: number | null) { return x! / 2; }"),
        (".ts", "function target(a: number[]) { return a[0]! / 2; }"),
        (".js", "function target() { return {valueOf: () => 4} / 2; }"),
        (".js", "function target() { const y = 1; return y + {valueOf() { return 4; }} / 2; }"),
    )
    for extension, target in cases:
        source = target + "\nfunction neighbor() { return 42; }\n"
        assert extract_block(source, "target", extension) == target, target


def test_a_regex_after_a_block_or_a_prefix_bang_is_still_a_regex() -> None:
    """The `}` of a block, `!` before a literal and `return!/re/` keep their regex reading."""
    cases = (
        "function target(x) { return !/}/.test(x); }",
        "function target(x) { return!/}/.test(x); }",
        "function target(x) { if (x) { x = 1; }\n /}/.test(x); return 1; }",
        "function target(s) { return ({}) / 2 + s.replace(/}/g, ''); }",
    )
    for target in cases:
        source = target + "\nfunction neighbor() { return 42; }\n"
        assert extract_block(source, "target", ".js") == target, target


def test_the_prompt_parser_reads_the_source_in_the_files_own_language() -> None:
    """A C++ raw string holding `/*` must not hide the class the prompt names; the mask knows the language."""
    for name in ("error", "work"):
        source = f'const char *s = R"(text\n/* ordinary string contents\n)";\nclass {name} {{}};\n'
        assert extract_block(source, name, ".cpp").strip() == f"class {name} {{}}"
        parsed = _parse_via_regex(f"show {name} in a.cpp", source)
        assert parsed is not None and parsed.target_name == name
    plain = _parse_via_regex("show error in a.cpp", "class error {};\n")
    assert plain is not None and plain.target_name == "error"
    # a real C++ comment still hides a mention, a Java text block does too
    commented = _parse_via_regex("show the def return function in a.cpp", "/* def return */\nclass handle {};\n")
    assert commented is not None and commented.target_name != "return"
    java = 'class A {\n String s = """\n/* x\n""";\n}\nclass error {}\n'
    parsed = _parse_via_regex("show error in A.java", java)
    assert parsed is not None and parsed.target_name == "error"


# ── T93 follow-up: block or object, NOT or non-null — decided by the lexer's own bracket pairing ──

_JS_NEIGHBOR = "\nfunction neighbor() { return 42; }\n"


def _extracts_itself(target: str, extension: str = ".js") -> bool:
    return extract_block(target + _JS_NEIGHBOR, "target", extension) == target


def test_a_regex_after_the_closing_brace_of_a_block_is_still_a_regex() -> None:
    """`else { … }`, a labeled block, `if` with a `{` in a string or a comment, `return` + newline + block."""
    for target in (
        "function target(x) { if (x) { x = 1; } else { x = 2; } /}/.test(String(x)); return x; }",
        "function target(x) { label: { x = 1; } /}/.test(String(x)); return x; }",
        "function target(x) { return\n { x = 1; } /}/.test(String(x)); return x; }",
        'function target(x) { if (x) { const s = "={"; } /}/.test(String(x)); return x; }',
        "function target(x) { if (x) { /* = { */ x = 1; } /}/.test(String(x)); return x; }",
        "function target(x) { switch (x) { case 1: { x = 2; } } /}/.test(String(x)); return x; }",
    ):
        assert _extracts_itself(target), target


def test_a_slash_after_an_object_literal_is_a_division_even_with_a_brace_in_a_string_or_comment() -> None:
    for target in (
        'function target() { return {valueOf: () => 4, text: "{"} / 2; }',
        'function target() { return {value: "}"} / 2; }',
        "function target() { return {valueOf: () => 4 /* { */} / 2; }",
        "function target(c) { return c ? 1 : {valueOf: () => 4} / 2; }",
        "function target() { const o = {a: {b: 1} / 2}; return o; }",
    ):
        assert _extracts_itself(target), target


def test_a_not_after_a_condition_is_a_prefix_and_a_bang_after_an_operand_is_a_ts_assertion() -> None:
    for target in (
        "function target(x) { if (x)!/}/.test(String(x)); return 42; }",
        "function target(x) { for (; false;)!/}/.test(String(x)); return 42; }",
        "function target(x) { while (x)!/}/.test(String(x)); return 42; }",
        "function target(x) { return!/}/.test(x); }",
    ):
        assert _extracts_itself(target), target
    for target in (
        "function target(x: number | null) { return x ! / 2; }",
        "function target(x: number | null) { return x!! / 2; }",
        "function target(x: number | null) { return x /* valid */ ! / 2; }",
        "function target(f: () => number | null) { return f()! / 2; }",
    ):
        assert _extracts_itself(target, ".ts"), target


def test_the_bracket_pairing_is_linear_on_a_large_file() -> None:
    import time
    body = "".join(
        f"function f{i}(x) {{ if (x) {{ x = x / 2; }} else {{ x = {{a: 1}}.a / 3; }} "
        f"return /}}/.test(String(x)) ? {{k: \"{{\"}} : x++ / 2; }}\n" for i in range(1500)
    )
    started = time.monotonic()
    assert extract_block(body, "f1499", ".js").startswith("function f1499(x)")
    assert time.monotonic() - started < 5
