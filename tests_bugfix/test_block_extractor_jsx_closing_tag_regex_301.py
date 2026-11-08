"""tests_bugfix/test_block_extractor_jsx_closing_tag_regex_301.py

Bug: the JS/TS branch of `tools.block_extractor._lang_skip` accepted a `/`
as the start of a regex literal whenever the previous significant character
could precede one — and `<` was in that set, so `</li>` in a `.tsx` file was
read as a regex and skipped to the next `/` on the line. The `}` that closed
the outer brace went with it, the brace count ran one short, and the block
returned for the target swallowed every function after it: Gate 1's grounding
and the Coder's fetch got `Other` as part of `List`.

Field report: HARDEN-4 D1, reproduced on a `.tsx` file before the fix.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.block_extractor import extract_block  # noqa: E402


def test_jsx_list_component_stops_at_its_own_closing_brace():
    src = (
        "export function List({items}) {\n"
        "  return <ul>{items.map(i => <li key={i}>{i}</li>)}</ul>;\n"
        "}\n"
        "export function Other() { return 1; }\n"
    )
    blk = extract_block(src, "List", ".tsx")
    assert "Other" not in blk
    assert blk.rstrip().endswith("}")


def test_jsx_conditional_child_does_not_swallow_the_next_function():
    src = (
        "export function Cond({ok}) {\n"
        "  return <div>{ok && <b>yes</b>}</div>;\n"
        "}\n"
        "export function Other() { return 1; }\n"
    )
    blk = extract_block(src, "Cond", ".tsx")
    assert "Other" not in blk


def test_a_real_regex_literal_is_still_skipped():
    # The case the skip exists for: a `/` that really does open a regex must
    # still hide its braces from the block scan.
    src = (
        "export function Reg(s) {\n"
        "  const re = /ab+c/g;\n"
        "  return re.test(s);\n"
        "}\n"
    )
    assert "/ab+c/g;" in extract_block(src, "Reg", ".ts")


def test_a_self_closing_jsx_tag_does_not_swallow_the_following_function():
    # T85: `/>` after a `}` (here `<Foo bar={y} />`) is a self-closing tag, not
    # a regex opening; the `}` that closes the surrounding `{ok && ...}` must
    # still be counted so the block ends at its own function.
    src = (
        "export function A({ok}) {\n"
        "  return <div>{ok && <Foo bar={y} />}\n"
        "}\n"
        "export function Other() { return 1; }\n"
    )
    blk = extract_block(src, "A", ".tsx")
    assert "Other" not in blk
    assert blk.rstrip().endswith("}")


def test_a_typescript_comparison_with_a_regex_keeps_both_literals_and_excludes_the_next_function():
    # T85: a `<` used as a comparison before a regex literal must not be read
    # as a JSX closing tag and lose the `/…/` after it; both literals survive
    # and the block ends before the next function.
    src = (
        "function Cmp(a, b) {\n"
        "  return a < /x/.test(b) && b > /y/.test(a);\n"
        "}\n"
        "export function Other() { return 1; }\n"
    )
    blk = extract_block(src, "Cmp", ".ts")
    assert "/x/" in blk and "/y/" in blk
    assert "Other" not in blk
    assert blk.rstrip().endswith("}")
