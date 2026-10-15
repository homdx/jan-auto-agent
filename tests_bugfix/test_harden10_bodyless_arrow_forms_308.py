r"""tests_bugfix/test_harden10_bodyless_arrow_forms_308.py — HARDEN-10 A: a bodyless arrow
function is found when its parameter list is empty, wraps over lines, or has a return type
or type parameters in front of the `=>`.

The two bodyless arrow patterns of HARDEN-6 C took the parameter list as
`\([^)\n]{1,400}\)` — at least one character, and no newline — so
`export const now = () => Date.now()` answered `''` ("not found"), which Gate 1 reads as a
hallucination and drops the task that cites it. `(): string => 'a'` and `<T>() => 1` had the
same shape, a class field `foo = () => 1` had it, a wrapped parameter list had it, and a
parameter list longer than 400 characters was lost the same way.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.block_extractor import extract_block  # noqa: E402


EXTENSIONS = (".js", ".jsx", ".ts", ".tsx")

# The ticket's table.
TICKET_ROWS = [
    ("export const now = () => Date.now();", "now", "() => Date.now()"),
    ("export const f = (): string => 'a';", "f", "(): string => 'a'"),
    ("const g = <T>() => 1;", "g", "<T>() => 1"),
    ("class A {\n  foo = () => 1;\n}", "foo", "foo = () => 1"),
    ("const h = (\n  a,\n  b\n) => a + b;", "h", "=> a + b"),
    ("const end = () => process.emit('x');", "end", "() => process.emit('x')"),
]


class TestTheTicketRows:
    """Every row of the ticket is found, in all four JS/TS extensions."""

    @pytest.mark.parametrize("ext", EXTENSIONS)
    @pytest.mark.parametrize("source, name, needle", TICKET_ROWS)
    def test_the_row_is_found(self, ext, source, name, needle):
        block = extract_block(source, name, ext)
        assert block, (ext, source, name)
        assert needle in block

    @pytest.mark.parametrize("ext", EXTENSIONS)
    def test_a_wrapped_parameter_list_is_one_block(self, ext):
        source = "const h = (\n  a,\n  b\n) => a + b;\n"
        assert extract_block(source, "h", ext) == source.rstrip("\n")

    @pytest.mark.parametrize("ext", EXTENSIONS)
    def test_the_return_type_is_inside_the_block(self, ext):
        assert extract_block("export const f = (): string => 'a';", "f", ext).strip() == \
            "export const f = (): string => 'a';"

    @pytest.mark.parametrize("ext", EXTENSIONS)
    def test_the_type_parameters_are_inside_the_block(self, ext):
        assert extract_block("const g = <T>() => 1;", "g", ext).strip() == "const g = <T>() => 1;"

    @pytest.mark.parametrize("ext", EXTENSIONS)
    def test_a_class_field_arrow_is_found(self, ext):
        assert extract_block("class A {\n  foo = () => 1;\n}", "foo", ext).strip() == \
            "foo = () => 1;"

    @pytest.mark.parametrize("ext", EXTENSIONS)
    def test_a_zero_argument_arrow_is_found(self, ext):
        # The parameter list is empty: the old class wanted at least one character.
        assert extract_block("export const now = () => Date.now();", "now", ext)


class TestAParameterListLongerThanTheOldBound:
    """The old class stopped at 400 characters, so a wide signature was lost."""

    LONG = "const big = (" + ", ".join(f"arg{i:02d}: number" for i in range(40)) + ") => arg00;"

    @pytest.mark.parametrize("ext", EXTENSIONS)
    def test_a_wide_parameter_list_is_found(self, ext):
        assert len(self.LONG.split("= (", 1)[1].split(") =>", 1)[0]) > 400
        block = extract_block(self.LONG, "big", ext)
        assert block
        assert "arg39: number" in block
        assert block.rstrip().endswith("arg00;")

    def test_a_wrapped_wide_parameter_list_is_found(self):
        params = ",\n  ".join(f"arg{i:02d}: number" for i in range(40))
        source = f"const wide = (\n  {params}\n) => arg00;\n"
        block = extract_block(source, "wide", ".ts")
        assert block and "arg39: number" in block


class TestNothingThatWasFoundChanged:
    """HARDEN-4 D2 and HARDEN-6 C: a block found today comes back from the same place."""

    @pytest.mark.parametrize("ext", EXTENSIONS)
    @pytest.mark.parametrize("source, name, expected", [
        ("const double = (x) => x * 2;", "double", "const double = (x) => x * 2;"),
        ("const clamp = (a, b) => Math.min(a, b);", "clamp",
         "const clamp = (a, b) => Math.min(a, b);"),
        ("const clamp = (a, b) =>\n  Math.min(a, b);", "clamp",
         "const clamp = (a, b) =>\n  Math.min(a, b);"),
        ("const f = <T>(x: T): T => x;", "f", "const f = <T>(x: T): T => x;"),
        ("const async_add = async (a, b) => a + b;", "async_add",
         "const async_add = async (a, b) => a + b;"),
        ("const id = x => x;", "id", "const id = x => x;"),
        ("let f = async (x) => x * 2;", "f", "let f = async (x) => x * 2;"),
        ("var F = func(a) { return a; }", "F", "var F = func(a) { return a; }"),
        ("export const n = (a) => { return a + 1; };", "n",
         "export const n = (a) => { return a + 1; }"),
    ])
    def test_the_bodyless_forms_are_unchanged(self, ext, source, name, expected):
        assert extract_block(source, name, ext).strip() == expected

    @pytest.mark.parametrize("ext", EXTENSIONS)
    def test_a_class_field_is_unchanged(self, ext):
        source = "class A {\n  handle = (e) => e + 1;\n}\n"
        assert extract_block(source, "handle", ext).strip() == "handle = (e) => e + 1;"

    @pytest.mark.parametrize("ext", EXTENSIONS)
    def test_a_braced_body_wins_over_a_bodyless_earlier_line(self, ext):
        source = "const x = (a, b) => a + b;\nfunction x(a) {\n  return a;\n}\n"
        block = extract_block(source, "x", ext)
        assert "function x(a) {" in block


class TestNotDefinitions:
    @pytest.mark.parametrize("ext", EXTENSIONS)
    def test_a_plain_value_is_not_a_definition(self, ext):
        assert extract_block("const n = 1;", "n", ext) == ""

    @pytest.mark.parametrize("ext", EXTENSIONS)
    def test_a_call_is_not_a_bodyless_definition(self, ext):
        assert extract_block("const r = now();", "now", ext) == ""

    @pytest.mark.parametrize("ext", EXTENSIONS)
    def test_a_zero_argument_call_is_not_a_definition(self, ext):
        assert extract_block("const r = now();", "now", ext) == ""


class TestTheScanStaysLinear:
    """HARDEN-6 C: a data module of records must not make one extraction quadratic."""

    def test_the_scan_scales_linearly_not_quadratically(self):
        import time

        def build(n):
            return "".join(
                f"const rec{i} = (id) => {{ return id * 2; }};\n" for i in range(n)
            )

        def elapsed(n):
            source = build(n)
            started = time.perf_counter()
            assert extract_block(source, "rec0", ".js")
            return time.perf_counter() - started

        elapsed(5000)  # warm up before timing
        small = max(elapsed(5000), 1e-6)
        big = elapsed(20000)
        # Four times the source costs about four times as long. The old bounded class
        # re-scanned the whole tail at every line, which would make it a hundred times.
        assert big / small < 40.0
