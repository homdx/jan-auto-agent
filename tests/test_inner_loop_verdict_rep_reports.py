"""REP-VERDICT: an approval the reply takes back, and a verdict word wrapped in markdown or a label, are rejections."""
import pytest

from tools.auto.inner_loop import _parse_verdict_soft


# A: the APPROVED / OK token approved at once; what followed it was never read (rep3 VD-3, rep4 A).
@pytest.mark.parametrize("reply", [
    "APPROVED\n\nREVISE: the date is wrong.\n- fix the timeline",
    "Approved. Reject.",
    "approved: false",
    "Approved: No",
    "APPROVED: NO",
    "approved = false",
    "approved - false",
    "Approved: No — the date is wrong",
    "APPROVED if the date is fixed.",
    "Approved once the typo is fixed.",
    "APPROVED. Actually no, the date is wrong.",
    "OK: no",
])
def test_approval_taken_back_is_a_rejection(reply):
    approved, reason, unparseable = _parse_verdict_soft(reply)
    assert (approved, unparseable) == (False, False)
    assert reason


def test_revise_line_after_approved_is_the_reason():
    assert _parse_verdict_soft("APPROVED\n\nREVISE: the date is wrong.") == (False, "the date is wrong.", False)


# B: a wrapped or labelled verdict word was unparseable and passed on fail-open (rep4 B).
@pytest.mark.parametrize("reply", [
    "**REVISE**", "**REVISE**: fix the date", "**REVISE:** fix the date", "__REVISE__", "`REVISE`",
    "*REVISE*", "**REJECT**", "**NO**", "**NOT APPROVED**", "# REVISE", "> REVISE: fix x",
    "- REVISE: fix x", "1. REVISE: fix x", "[REVISE] fix x", "(REVISE) fix x", "Verdict: REVISE",
    "**Verdict:** REJECT — breaks API", "VERDICT: REJECT: wrong", "Decision: REVISE", "Answer: NO",
    "Final answer: REJECT", "Result: NOT APPROVED", "Status: needs revision",
])
def test_wrapped_verdict_word_is_read(reply):
    approved, _reason, unparseable = _parse_verdict_soft(reply)
    assert (approved, unparseable) == (False, False)


def test_wrapped_reason_drops_the_wrapper():
    assert _parse_verdict_soft("**REVISE**: fix the date") == (False, "fix the date", False)


# The approvals the two rules must leave alone.
@pytest.mark.parametrize("reply", [
    "APPROVED", "APPROVED.", "OK", "APPROVED. Save the file.", "APPROVED. Fix nothing.",
    "Approved - no issues found", "APPROVED: no changes needed", "APPROVED: not a problem",
    "APPROVED after a careful read.", "**APPROVED**", "Verdict: APPROVED", "- APPROVED",
    "Status: OK", "1. No issues found.", "APPROVED\n- the pacing is good\n- the dialogue reads well",
])
def test_plain_and_wrapped_approvals_stay_approvals(reply):
    assert _parse_verdict_soft(reply) == (True, "", False)
