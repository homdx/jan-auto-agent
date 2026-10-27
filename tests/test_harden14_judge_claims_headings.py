"""tests/test_harden14_judge_claims_headings.py — HARDEN-14 D: a `## Claims:` section, and
`### Claims` / `#### Claims`, are read as a claims section.

`ticket_claims("## Claims:\n\n- [new] a\n")` returned `[]`, as did the same under `###` or `####`,
as did a file with no section at all — so the judge scored nothing and said nothing. A heading of
level 2 to 4 whose text is `Claims` with an optional trailing colon opens the section. HARDEN-20 E
opened it to all six markdown levels, because the reader is now the one the summary uses too and
the ceiling was an arbitrary 4.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location("judge", ROOT / "scripts" / "judge_epic_round.py")
judge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(judge)


class TestTheHeadingLevelsOpenTheSection:
    """Level 2 to 6, with or without a trailing colon."""

    @pytest.mark.parametrize("heading", [
        "## Claims:",
        "### Claims",
        "#### Claims",
        "#### Claims:",
        "## Claims",
        "## claims",
        "### CLAIMS:",
        "##### Claims",
        "###### Claims",
    ])
    def test_the_claim_is_read(self, heading):
        claims = judge.ticket_claims(f"{heading}\n\n- [new] `a` returns 1.\n", "t.md")
        assert len(claims) == 1
        assert claims[0] == {"claim": "`a` returns 1.", "expect": "new"}

    def test_the_suffix_and_the_case_already_did(self):
        claims = judge.ticket_claims("## Claims (3)\n\n- [still] `a` writes.\n", "t.md")
        assert [c["expect"] for c in claims] == ["still"]


class TestASiblingHeadingEndsTheSection:
    def test_a_level3_sibling_does_not_continue_it(self):
        text = (
            "### Claims\n"
            "\n"
            "- [new] `a` returns 1.\n"
            "\n"
            "### Rules\n"
            "- [new] prose after the sibling is not a claim.\n"
        )
        claims = judge.ticket_claims(text, "t.md")
        assert len(claims) == 1

    def test_a_deeper_heading_does_not_end_it(self):
        text = "## Claims\n\n- [new] `a` returns 1.\n\n#### Note\n- [new] still the section.\n"
        claims = judge.ticket_claims(text, "t.md")
        assert len(claims) == 2


class TestTheRefusals:
    def test_a_heading_with_no_claim_refuses_and_names_the_line(self):
        with pytest.raises(ValueError, match=r"t\.md:1: the Claims section has no claim"):
            judge.ticket_claims("## Claims\n\n", "t.md")
        with pytest.raises(ValueError, match=r"t\.md:1: the Claims section has no claim"):
            judge.ticket_claims("## Claims:\n", "t.md")
        with pytest.raises(ValueError, match=r"t\.md:3: the Claims section has no claim"):
            judge.ticket_claims("# T\n\n#### Claims:\n", "t.md")

    @pytest.mark.parametrize("text", ["", "## No claims here\n",
                                      "## Claimants\n\n- [new] a\n", "```md\n## Claims\n```\n"])
    def test_a_ticket_without_a_claims_heading_still_has_no_claims(self, text):
        assert judge.ticket_claims(text, "t.md") == []
