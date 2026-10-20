"""tests/test_verdict_review47_blank_runs_and_no_problem_answer.py — CC review fixes 47: two leftovers of HARDEN-26.

Each reproduced on 8d561bb:

* A: `_EN_CLAUSE_CAVEAT` got `\\s*` twice in a row after the stop, `-\\s+` before a `\\s*` and a
  `\\s*` before the parenthesis, so a run of blanks was split every way before the search gave
  up: "Looks good." and three thousand blank lines took 95 s (0.12 s on f7d2e01), "LGTM" and
  twenty thousand spaces 18 s — every validator, Gate 2 and the plan check read that reply.
* B: the `No` that answers an approval question was taken for a no even when it opens the
  "No <problem>" approval: "Approved? No issues." and "OK? No problem." rejected, and the Coder
  was told to fix "issues." — read alone, "No issues." approves (HARDEN-10 C).
"""

from __future__ import annotations

import time

import pytest

from tools.auto.inner_loop import _parse_verdict_soft


# ── A: a run of blanks is read once ──────────────────────────────────────────


@pytest.mark.parametrize("reply", [
    "Looks good." + "\n" * 3000 + "Thanks",
    "LGTM" + " " * 20000 + "x",
    "LGTM -" + " " * 20000 + "x",
    "APPROVED," + " " * 60000 + "x",
], ids=["blank-lines", "spaces", "hyphen-then-spaces", "comma-then-spaces"])
def test_a_long_run_of_blanks_is_read_at_once(reply):
    """95 s, 18 s, 19 s and 40 s on 8d561bb; a few milliseconds each with the fix."""
    t0 = time.monotonic()
    _parse_verdict_soft(reply)
    assert time.monotonic() - t0 < 5.0


@pytest.mark.parametrize("reply,reason", [
    ("Looks good,\n\n   but the ring is stolen.", "the ring is stolen."),
    ("Looks good.\n\n\nHowever, the ring is stolen.", "the ring is stolen."),
    ("Looks good\n\n  \n but the ring is stolen.", "the ring is stolen."),
    ("Looks good —   but the ring is stolen.", "the ring is stolen."),
    ("Looks good -\nbut the ring is stolen.", "the ring is stolen."),
    ("Looks good -\t but the ring is stolen.", "the ring is stolen."),
    ("Looks good (  but the ring is stolen)", "the ring is stolen)"),
])
def test_a_contrast_after_blanks_and_newlines_still_opens_the_clause(reply, reason):
    """The last newline before the contrast is the boundary now: the match ends where it did."""
    assert _parse_verdict_soft(reply) == (False, reason, False)


# ── B: "No <problem>" after the question is the approval it says ────────────


@pytest.mark.parametrize("reply", [
    "Approved? No issues.",
    "Approved? No issues found.",
    "APPROVED? NO ISSUES.",
    "OK? No problems.",
    "OK? No problem.",
    "OK?No issues",
    "OK? No complaints.",
    "Approved? No objections.",
    "Approved? No changes needed.",
    "Approved? No contradictions found.",
    "Approved? No major issues.",
    "Approved?? No problems found; the timeline is consistent.",
])
def test_an_approval_question_answered_with_no_problem_approves(reply):
    assert _parse_verdict_soft(reply) == (True, "", False)


@pytest.mark.parametrize("reply", ["Looks good? No complaints.", "LGTM? No objections from me."])
def test_a_praise_question_answered_with_no_problem_is_no_rejection(reply):
    """These failed open to an approval before HARDEN-26, and fail open again."""
    approved, _reason, unparseable = _parse_verdict_soft(reply)
    assert approved is True and unparseable is True


@pytest.mark.parametrize("reply,quoted", [
    ("Approved? No issues except the date.", "the date"),
    ("Approved? No problems found; one problem: the date.", "the date"),
    ("Approved? No blocking issues, ship it.", "blocking issues"),
    ("Approved? No, issues remain.", "issues remain."),
    ("Approved? No the test is wrong.", "the test is wrong."),
    ("Approved? No — the test is wrong.", "the test is wrong."),
    ("Approved? No.", "Approved? No."),
])
def test_a_no_that_is_not_the_no_problem_approval_still_rejects(reply, quoted):
    """A caveat after the "No <problem>", a "No," and a "No" before anything else stay rejections."""
    approved, reason, unparseable = _parse_verdict_soft(reply)
    assert approved is False and unparseable is False, reply
    assert quoted in reason, (reply, reason)
