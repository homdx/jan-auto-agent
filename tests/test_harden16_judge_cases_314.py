"""HARDEN-16 (314): judge-found cases — a name next to a quote far from the line start, a file of many tokens, void/await/typeof/new before a JS call."""
import pytest

from tools.block_extractor import extract_block
from tools.llm_stream import _region_at, mask_api_key

TOKEN = "ghp_" + "aB3dE5gH7jK9mN1pQ3sT5vW7yZ9bD1fH3jK5"


def test_region_at_finds_the_region_that_holds_an_index():
    regions = [(2, 5, "string"), (8, 12, "comment"), (20, 30, "string")]
    assert _region_at(regions, 1) is None
    assert _region_at(regions, 2) == (2, 5, "string")
    assert _region_at(regions, 4) == (2, 5, "string")
    assert _region_at(regions, 5) is None
    assert _region_at(regions, 11) == (8, 12, "comment")
    assert _region_at(regions, 29) == (20, 30, "string")
    assert _region_at(regions, 30) is None
    assert _region_at([], 3) is None


def test_a_comparison_against_a_quoted_literal_stays_masked_deep_in_a_file():
    filler = "".join(f"x{i} = {i}\n" for i in range(300))
    out = mask_api_key(filler + f'if "abc" == {TOKEN}:\n    pass\n')
    assert TOKEN not in out


def test_a_name_comparison_stays_as_written_deep_in_a_file():
    filler = "".join(f"x{i} = {i}\n" for i in range(300))
    line = f"if {TOKEN} in valid_keys:\n"
    assert mask_api_key(filler + line).endswith(line)


def test_many_names_and_values_in_one_file_are_decided_one_by_one():
    text = "".join(f"if {TOKEN} in keys{i}:\n    k{i} = '{TOKEN}'\n" for i in range(50))
    out = mask_api_key(text)
    assert out.count("if " + TOKEN + " in keys") == 50
    assert out.count(TOKEN) == 50


@pytest.mark.parametrize("lead", ["void ", "await ", "typeof ", "delete ", "new ", "yield ", "return ", "throw "])
def test_an_operator_before_a_js_call_is_a_call_not_a_declaration(lead):
    src = f"function outer() {{\n  {lead}helperFn(a);\n}}\n"
    assert extract_block(src, "helperFn", ".ts") == ""
