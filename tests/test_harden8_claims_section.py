"""HARDEN-8 C (306): a numbered claims list is read, and a fenced `## Claims` is not a section.

`ticket_claims`'s docstring promises that a list item without its word in brackets is a refusal
naming the line — "the section is a claims list, not prose". `_LIST_ITEM` was `^\\s*[-*+]\\s+`, so
only dash, star and plus items counted and a numbered item fell in neither branch:

* `- [new] a` / `2. [still] b` / `3. [gone] c` gave one claim and dropped the other two silently;
* `1. [new] a` / `2. [still] b` gave no claims at all, and no refusal either;
* a `## Claims` heading inside a fenced block was a section, and its lines were parsed as claims.

A numbered item is now a list item like the others — with its word in brackets it is a claim,
without one a refusal naming the line — and a line inside a fence is neither a heading nor a
claim. `tools/contest/export.py`'s `ticket_has_claims` reads the heading the same way, so the two
parsers that read the section agree.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

spec = importlib.util.spec_from_file_location("judge_epic_round_h8", ROOT / "scripts" / "judge_epic_round.py")
judge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(judge)

from tools.contest import export  # noqa: E402  (the other parser that reads the section)


def _claims(text):
    return judge.ticket_claims(text, "306-x.md")


# ------------------------------------------------------------------ a numbered item is a list item

def test_a_numbered_list_is_a_claims_list():
    """The reproduction: `- [new] a` / `2. [still] b` / `3. [gone] c` is three claims, not one."""
    text = "## Claims\n- [new] a\n2. [still] b\n3. [gone] c\n"
    assert _claims(text) == [{"claim": "a", "expect": "new"},
                             {"claim": "b", "expect": "still"},
                             {"claim": "c", "expect": "gone"}]


def test_a_numbered_list_alone_is_a_claims_list():
    """`1. [new] a` / `2. [still] b` gave no claims and no refusal: the whole list was gone."""
    text = "## Claims\n\n1. [new] a\n2. [still] b\n"
    assert _claims(text) == [{"claim": "a", "expect": "new"},
                             {"claim": "b", "expect": "still"}]


def test_a_numbered_item_without_its_word_is_a_refusal_naming_the_line():
    """The docstring's promise, kept for the numbered form: a refusal, not a silent skip."""
    text = "## Claims\n\n1. [new] a\n2. b\n"
    with pytest.raises(ValueError, match=r"306-x\.md:4: a claim names its word in brackets"):
        _claims(text)
    text = "## Claims\n\n1. a\n"
    with pytest.raises(ValueError, match=r"306-x\.md:3: a claim names its word in brackets"):
        _claims(text)


def test_a_numbered_item_with_an_unknown_word_is_a_refusal_naming_the_line():
    text = "## Claims\n\n1. [ne]w a\n"
    with pytest.raises(ValueError, match=r"306-x\.md:3: '\[ne\]w' is not one of"):
        _claims(text)


def test_a_closing_parenthesis_is_a_numbered_item_too():
    text = "## Claims\n\n1) [new] a\n2) [still] b\n"
    assert _claims(text) == [{"claim": "a", "expect": "new"},
                             {"claim": "b", "expect": "still"}]


# ------------------------------------------------------------------ a fenced heading is not a section

def test_a_heading_inside_a_fence_is_not_a_section():
    """The reproduction: a fence holding `## Claims` and a bullet gave one claim."""
    text = "a fence:\n```\n## Claims\n- [new] a\n```\n"
    assert _claims(text) == []


def test_a_heading_inside_a_fence_is_not_a_section_for_the_other_parser(tmp_path):
    """`ticket_has_claims` reads the heading the way `ticket_claims` does."""
    (tmp_path / "epic-tasks").mkdir()
    (tmp_path / "epic-tasks" / "306-fenced.md").write_text(
        "## Why\n\n```markdown\n## Claims\n- [new] a\n```\n", encoding="utf-8")
    assert export.ticket_has_claims(str(tmp_path / "out"), "306-fenced.md") is False
    (tmp_path / "epic-tasks" / "306-real.md").write_text(
        "## Why\n\n```markdown\n## Notes\n```\n\n## Claims\n\n- [new] a\n", encoding="utf-8")
    assert export.ticket_has_claims(str(tmp_path / "out"), "306-real.md") is True


def test_a_claim_inside_a_fence_inside_the_section_is_not_a_claim():
    """A section that documents its own shape: the fenced lines are not claims."""
    text = ("## Claims\n\n- [new] `a` returns 1.\n\n"
            "The shape of a claim:\n\n```\n- [fixed] `b` returns 2.\n```\n")
    assert _claims(text) == [{"claim": "`a` returns 1.", "expect": "new"}]


def test_an_unclosed_fence_hides_the_rest_of_the_section():
    """A fence that never closes runs to the end of the document, as in CommonMark: the lines
    after it are inside it, so they are not claims. Nothing is refused and nothing is misread —
    the fail-open answer is fewer claims, never a claim made of code."""
    text = "## Claims\n\n- [new] `a` returns 1.\n\n```\n- [fixed] `b` returns 2.\n"
    assert _claims(text) == [{"claim": "`a` returns 1.", "expect": "new"}]


# ------------------------------------------------------------------ what must not change

def test_a_numbered_list_after_another_heading_is_not_a_refusal():
    """`- [new] …` claims, then a numbered list of notes under another heading: unchanged."""
    text = ("## Claims\n\n"
            "- [new] `a` returns 1.\n"
            "- [fixed] `a` returns 2.\n"
            "\n## Notes\n\n"
            "1. a note about the first\n"
            "2. a note about the second\n")
    assert _claims(text) == [{"claim": "`a` returns 1.", "expect": "new"},
                             {"claim": "`a` returns 2.", "expect": "fixed"}]


def test_the_shipped_bullet_forms_are_unchanged():
    """The four words, a wrapped bullet and prose after the next heading: as they were."""
    text = "\n".join([
        "## Claims",
        "",
        "- [new] `a` returns 1.",
        "- [fixed] `a` returns 2.",
        "- [still] `a` never writes.",
        "- [gone] `a` is imported.",
        "",
        "- [new] `b` returns",
        "    3 on the slow path.",
        "",
        "## Rules",
        "- [new] `c` is prose after the section, not a claim.",
    ])
    claims = _claims(text)
    assert [c["expect"] for c in claims] == ["new", "fixed", "still", "gone", "new"]
    assert claims[-1]["claim"] == "`b` returns 3 on the slow path."
    assert len(claims) == 5


def test_a_ticket_without_the_section_has_no_claims():
    assert _claims("## Why\n\nnothing here\n") == []
    assert _claims("") == []
    assert _claims("## Claims\n\n") == []
