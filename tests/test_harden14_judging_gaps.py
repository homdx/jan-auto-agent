"""HARDEN-14 judging gaps: the NOT family's remaining approvals, a destructor named by its class, a union type member."""
import pytest

from tools.auto.inner_loop import _parse_verdict_soft
from tools.block_extractor import extract_block


@pytest.mark.parametrize("reply", [
    "Not a single contradiction found.",
    "Not in contradiction with the bible",
    "Not in violation of the style guide.",
    "No new errors",
    "No remaining issues",
    "No obvious problem found",
])
def test_a_denied_problem_is_an_approval(reply):
    assert _parse_verdict_soft(reply)[0] is True


@pytest.mark.parametrize("reply", [
    "Not a single detail matches the bible.",
    "Not in character for Ana",
    "Not in line with the canon.",
])
def test_a_single_or_in_that_names_no_problem_stays_a_rejection(reply):
    assert _parse_verdict_soft(reply)[0] is False


@pytest.mark.parametrize("ext", [".h", ".cpp", "cpp"])
def test_a_destructor_is_found_by_its_class_name_when_nothing_else_is(ext):
    assert extract_block("~I() = default;\n", "I", ext) == "~I() = default;"
    assert extract_block("virtual ~I() noexcept;\n", "I", ext).endswith(";")


def test_the_class_still_wins_over_its_destructor():
    src = "struct S {\n  ~S() = default;\n};\n"
    assert extract_block(src, "S", "cpp").startswith("struct S")


@pytest.mark.parametrize("src, name", [
    ("interface I {\n  onDone: () => void | null;\n}\n", "onDone"),
    ("interface I {\n  onDone: () => Foo | Bar<T>[];\n}\n", "onDone"),
])
def test_a_type_member_with_a_union_is_not_a_definition(src, name):
    assert extract_block(src, name, "ts") == ""


def test_an_arrow_whose_value_uses_a_bar_is_still_a_definition():
    assert "b(c)" in extract_block("const o = {\n  f: () => a | b(c),\n};\n", "f", "ts")
