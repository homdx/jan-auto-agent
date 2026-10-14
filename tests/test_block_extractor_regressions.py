"""tests/test_block_extractor_regressions.py — HARDEN-4 D1/D2/D4.

Three defects in what `extract_block` answers for a non-Python file, each of
them a wrong answer nobody had tested for:

* **D1** — after `<` the JS branch of `_lang_skip` accepted a `/` as a regex
  literal, so `</li>` in a `.tsx` file was read as one and skipped to the next
  `/` on the line: the `}` closing the outer brace went with it, and every
  function after the target came out as part of its block.
* **D2** — a definition with no `{ … }` body (`const double = (x) => x * 2;`,
  `const f = function () {}`, `module.exports = function m() {}`,
  `var F = func() {}`, `fun ff() = 1`) answered "not found", so Gate 1 Stage A
  rejected a symbol that exists as a hallucination.
* **D4** — a `#define` whose body continues on a backslash ended the C block
  at the macro's own `}`.

Every test here also pins what must NOT change: a real regex literal is still
skipped (the case the lexer skip was made for), a braced body is unchanged, a
bare call site still yields nothing, and a macro without a continuation is
unchanged.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.block_extractor import extract_block, get_context_lines  # noqa: E402


# ── D1: a JSX closing tag is not a regex literal ─────────────────────────────

JSX_LIST = (
    "export function List({items}) {\n"
    "  return <ul>{items.map(i => <li key={i}>{i}</li>)}</ul>;\n"
    "}\n"
    "export function Other() { return 1; }\n"
)


def test_jsx_closing_tag_does_not_swallow_the_following_function():
    blk = extract_block(JSX_LIST, "List", ".tsx")
    assert "Other" not in blk
    assert blk.rstrip().endswith("}")
    assert blk.count("}") == blk.count("{")  # balanced: the outer brace came back


def test_jsx_conditional_child_closing_tag_is_not_a_regex():
    src = (
        "export function Cond({ok}) {\n"
        "  return <div>{ok && <b>yes</b>}</div>;\n"
        "}\n"
        "export function Other() { return 1; }\n"
    )
    blk = extract_block(src, "Cond", ".tsx")
    assert "Other" not in blk
    assert "</div>;" in blk


def test_real_regex_literal_after_an_operator_is_still_skipped():
    # The case the JS lexer skip exists for: a `/` that really does open a regex.
    src = (
        "export function Reg(s) {\n"
        "  const re = /ab+c/g;\n"
        "  return re.test(s);\n"
        "}\n"
        "export function Other() { return 1; }\n"
    )
    blk = extract_block(src, "Reg", ".ts")
    assert "/ab+c/g;" in blk
    assert "Other" not in blk


def test_regex_after_a_return_statement_is_still_skipped():
    src = (
        "function check(s) {\n"
        "  return /^[a-z]+$/.test(s);\n"
        "}\n"
    )
    assert "/^[a-z]+$/.test(s);" in extract_block(src, "check", ".js")


# ── D2: a definition with no braced body is still a definition ───────────────

BODYLESS_TABLE = [
    ("const double = (x) => x * 2;\n", "double", ".ts"),
    ("const clamp = (a, b) =>\n  a + b;\n", "clamp", ".ts"),
    ("const f = function () {}\n", "f", ".js"),
    ("module.exports = function m() {}\n", "m", ".js"),
    ("var F = func() {}\n", "F", ".go"),
    ("fun ff() = 1\n", "ff", ".kt"),
]


def test_every_bodyless_form_returns_its_definition():
    for source, name, ext in BODYLESS_TABLE:
        blk = extract_block(source, name, ext)
        assert blk, f"{name!r} ({ext}) was not found in {source!r}"


def test_bodyless_arrow_expression_returns_the_whole_statement():
    blk = extract_block("const double = (x) => x * 2;\n", "double", ".ts")
    assert blk.strip() == "const double = (x) => x * 2;"


def test_bodyless_arrow_continuation_line_stays_in_its_own_block():
    src = "const clamp = (a, b) =>\n  a + b;\n"
    blk = extract_block(src, "clamp", ".ts")
    # Ends at the `;`, like a braced block ends at its `}`: no trailing newline.
    assert blk == "const clamp = (a, b) =>\n  a + b;"
    assert "a + b;" in blk


def test_bodyless_kotlin_expression_body_stops_at_end_of_line():
    src = "fun ff() = 1\nfun gg() = 2\n"
    blk = extract_block(src, "ff", ".kt")
    assert blk.strip() == "fun ff() = 1"
    assert "gg" not in blk


def test_bodyless_definition_does_not_run_into_the_next_one():
    src = "const a = (x) => x + 1;\nconst b = (x) => x + 2;\n"
    blk = extract_block(src, "a", ".ts")
    assert "b" not in blk.replace("const a", "")


def test_a_braced_body_is_still_extracted_to_its_closing_brace():
    src = "const add = (a, b) => {\n    return a + b;\n};\n"
    blk = extract_block(src, "add", ".js")
    assert blk == "const add = (a, b) => {\n    return a + b;\n}"


def test_unknown_name_still_returns_empty():
    assert extract_block("const double = (x) => x * 2;\n", "doesNotExist", ".ts") == ""


def test_a_bare_call_site_is_still_not_a_definition():
    # The AUTO-BUG this whole strategy was hardened against: only a declaration
    # head answers, never a call.
    src = "function main() {\n    helper(1, 2);\n}\n"
    assert extract_block(src, "helper", ".js") == ""


def test_a_bodyless_arrow_still_cannot_hijack_an_unrelated_later_block():
    src = (
        "const add = (a, b) => a + b;\n"
        "\n"
        "function unrelatedThing() {\n"
        "    doStuffA();\n"
        "}\n"
    )
    blk = extract_block(src, "add", ".js")
    assert "doStuffA" not in blk


def test_gate1_existence_check_now_finds_a_bodyless_definition():
    # The caller D2 was filed against: Stage A's symbol anchor.
    from tools.auto.gate1_filter import Gate1Filter

    gate = Gate1Filter.__new__(Gate1Filter)
    gate._task_mode = "code"
    gate._max_block_chars = 4000
    gate._max_context_lines = 10

    class _Loc:
        new_file = False
        file = "a.ts"
        symbol = "double"
        line_start = None
        line_end = None

    class _Cand:
        cluster = ""
        cited_location = _Loc()

    ok, _reason, block = gate._check_existence(_Cand(), PROJECT_ROOT / "tools")
    assert ok is False  # there is no tools/a.ts — the path check comes first
    assert "not found" in block or block == ""

    # The same call against a real file answers on the symbol, not the path.
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        (Path(td) / "a.ts").write_text("const double = (x) => x * 2;\n", encoding="utf-8")
        ok, reason, block = gate._check_existence(_Cand(), Path(td))
    assert ok is True, reason
    assert "const double" in block


def test_context_lines_finds_a_bodyless_definition():
    src = "// a comment right above\nconst double = (x) => x * 2;\n"
    ctx = get_context_lines(src, "double", before=1, file_ext=".ts")
    assert "a comment right above" in ctx


# ── D4: a multi-line C macro belongs to the macro ────────────────────────────

C_MACRO_SRC = (
    "int f(int x) {\n"
    "#define SW(a,b) do { \\\n"
    "  a = b; \\\n"
    "} while (0)\n"
    "  SW(x, x);\n"
    "  return x;\n"
    "}\n"
)


def test_multiline_c_macro_does_not_cut_the_block():
    blk = extract_block(C_MACRO_SRC, "f", ".c")
    assert "return x;" in blk
    assert blk.rstrip().endswith("}")
    assert "while (0)" in blk


def test_c_macro_without_a_continuation_is_unchanged():
    src = "int g(int x) {\n#define PLAIN 1\n  return x + PLAIN;\n}\n"
    blk = extract_block(src, "g", ".c")
    assert "return x + PLAIN;" in blk
    assert blk.rstrip().endswith("}")


def test_c_macro_at_end_of_block_is_not_run_past_the_closing_brace():
    src = "int h(int x) {\n#define LOOP(n) for (int i = 0; i < n; i++) \\\n  do { x++; } while (0)\n  LOOP(3);\n  return x;\n}\nint after(int x) {\n  return x;\n}\n"
    blk = extract_block(src, "h", ".c")
    assert "int after" not in blk
    assert "LOOP(3);" in blk


def test_a_class_field_arrow_function_is_a_definition():
    """D2: `handle = (e) => e + 1;` inside a class has no `const`/`let`/`var` and no braces; it is still the definition of `handle`."""
    from tools.block_extractor import extract_block
    src = "class A {\n  handle = (e) => e + 1;\n  other = 2;\n}\n"
    block = extract_block(src, "handle", ".ts")
    assert "handle = (e) => e + 1;" in block and "other" not in block
    wrapped = "class B {\n  private static run = async (a: number): Promise<number> =>\n    a + 1;\n  next = 3;\n}\n"
    block = extract_block(wrapped, "run", ".ts")
    assert "a + 1;" in block and "next" not in block
    # a plain field that is not an arrow function is not a definition of a function
    assert extract_block("class C {\n  value = 5;\n  other = 2;\n}\n", "value", ".ts") == ""
