"""tests_bugfix/test_harden9_property_arrow_block_307.py — HARDEN-9 E: an arrow function that
is a property of an object literal is found again.

HARDEN-4 D2 added `const f = (x) => {…}`, a class field and a method, and the object-literal
property is the one shape left behind: `name: (x) => {…}` has no `const` and no `function`
in front of it, so no pattern reached it and it answered `''` — "not found", which Gate 1
reads as a hallucination and drops the task that cites it. Handlers, reducers, route tables
and action maps are written this way.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.block_extractor import extract_block  # noqa: E402

SRC = """const api = {
  fetchUser: (id) => {
    const user = load(id);
    return { ...user };
  },
  save: async (u) => {
    await persist(u);
  },
  plain(x) {
    return x;
  },
  old: function (y) {
    return y + 1;
  },
  oneLiner: (x) => x * 2,
  bare: id => load(id),
  opt?: (id: string): User => load(id),
  typed: <T>(x: T): T => x,
  number: 5,
  value: someVar,
  wrap: {
    inner: (x) => {
      return map(x);
    },
  },
};

export const top = (z) => {
  return z;
};

function real(name) {
  return name;
}
"""

EXTENSIONS = (".js", ".ts", ".jsx", ".tsx")


class TestTheTicketRows:
    @pytest.mark.parametrize("ext", EXTENSIONS)
    def test_a_braced_property_arrow_is_found(self, ext):
        block = extract_block(SRC, "fetchUser", ext)
        assert block
        assert "fetchUser: (id) => {" in block
        assert "load(id)" in block
        assert block.rstrip().endswith("}")

    @pytest.mark.parametrize("ext", EXTENSIONS)
    def test_an_async_braced_property_arrow_is_found(self, ext):
        block = extract_block(SRC, "save", ext)
        assert block
        assert "save: async (u) => {" in block
        assert "await persist(u);" in block
        assert block.rstrip().endswith("}")

    @pytest.mark.parametrize("ext", EXTENSIONS)
    def test_the_comma_is_not_the_end_of_the_body(self, ext):
        # The property's block ends at its own `}`, not at the comma of the literal.
        assert extract_block(SRC, "fetchUser", ext).rstrip().endswith("}")
        assert "save: async" not in extract_block(SRC, "fetchUser", ext)
        assert "plain(x)" not in extract_block(SRC, "fetchUser", ext)

    @pytest.mark.parametrize("ext", EXTENSIONS)
    def test_the_method_and_the_function_expression_are_still_found(self, ext):
        assert extract_block(SRC, "plain", ext)
        assert extract_block(SRC, "old", ext)

    @pytest.mark.parametrize("ext", EXTENSIONS)
    def test_the_export_const_arrow_is_still_found(self, ext):
        block = extract_block(SRC, "top", ext)
        assert block and "export const top = (z) => {" in block

    @pytest.mark.parametrize("ext", EXTENSIONS)
    def test_a_nested_property_arrow_is_found(self, ext):
        block = extract_block(SRC, "inner", ext)
        assert block and "inner: (x) => {" in block and "map(x)" in block


class TestTheBodylessForm:
    @pytest.mark.parametrize("ext", EXTENSIONS)
    def test_a_single_expression_property_is_found(self, ext):
        assert extract_block(SRC, "oneLiner", ext).strip() == "oneLiner: (x) => x * 2"

    @pytest.mark.parametrize("ext", EXTENSIONS)
    def test_a_bare_parameter_property_is_found(self, ext):
        assert extract_block(SRC, "bare", ext).strip() == "bare: id => load(id)"

    @pytest.mark.parametrize("ext", EXTENSIONS)
    def test_an_optional_typed_property_is_found(self, ext):
        assert extract_block(SRC, "opt", ext).strip() == "opt?: (id: string): User => load(id)"

    @pytest.mark.parametrize("ext", EXTENSIONS)
    def test_a_generic_property_is_found(self, ext):
        assert extract_block(SRC, "typed", ext).strip() == "typed: <T>(x: T): T => x"

    @pytest.mark.parametrize("ext", EXTENSIONS)
    def test_the_comma_ends_the_one_liner(self, ext):
        block = extract_block(SRC, "oneLiner", ext)
        assert block.count("\n") <= 1
        assert "bare:" not in block
        assert not block.rstrip().endswith(",")


class TestNotADefinition:
    @pytest.mark.parametrize("ext", EXTENSIONS)
    @pytest.mark.parametrize("name", ["number", "value", "someVar", "load", "persist", "map"])
    def test_a_property_whose_value_is_not_a_function_is_not_a_definition(self, ext, name):
        assert extract_block(SRC, name, ext) == ""

    def test_a_plain_variable_is_not_a_property(self):
        assert extract_block(SRC, "api", ".js") == ""

    def test_an_arrow_value_without_a_name_is_not_a_definition(self):
        assert extract_block(SRC, "x", ".ts") == ""


class TestNothingThatWasFoundChanged:
    def test_a_genuine_definition_beats_an_earlier_property(self):
        source = """const handlers = {
  submit: (e) => {
    return doIt(e);
  },
};

function submit(x) {
  return x + 1;
}
"""
        for ext in EXTENSIONS:
            block = extract_block(source, "submit", ext)
            assert block
            assert "function submit" in block
            assert "doIt" not in block

    def test_the_declaration_pass_still_comes_first(self):
        source = """declare function reset(): void;
const api = {
  reset: () => {
    return clear();
  },
};
"""
        for ext in EXTENSIONS:
            block = extract_block(source, "reset", ext)
            assert block
            assert "declare function reset(): void;" in block

    def test_a_bodyless_const_still_wins_over_a_property(self):
        source = """const api = {
  clamp: (a, b) => {
    return Math.min(a, b);
  },
};
const clamp = (a, b) => Math.min(a, b);
"""
        for ext in EXTENSIONS:
            block = extract_block(source, "clamp", ext)
            assert block
            assert "const clamp = (a, b) =>" in block

    def test_a_function_definition_in_the_same_file_is_unchanged(self):
        assert extract_block(SRC, "real", ".js") == "function real(name) {\n  return name;\n}"
