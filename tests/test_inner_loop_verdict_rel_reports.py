"""REL reports: a rejection buried mid-reply rejects, and praise and "No <x> found" approve."""
import time

import pytest

from tools.auto.inner_loop import _parse_verdict_soft


# REL-4 A–E: passed on fail-open before.
@pytest.mark.parametrize("reply,reason", [
    ("The story is consistent. Needs revision for the date.", "for the date."),
    ("Good work! REVISION NEEDED.", None),
    ("REVISION NEEDED: fix the date", "fix the date"),
    ("Changes required: fix the date", "fix the date"),
    ("The chapter is REJECTED — wrong character.", "wrong character."),
    ("Changes are REJECTED.", None),
    ("Request DENIED — plot hole.", "plot hole."),
    ("Denied — plot hole.", "plot hole."),
    ("LGTM. Revision needed for the ending.", "for the ending."),
])
def test_a_buried_rejection_rejects(reply, reason):
    approved, got, unparseable = _parse_verdict_soft(reply)
    assert (approved, unparseable) == (False, False)
    if reason is not None:
        assert got == reason


@pytest.mark.parametrize("reply", [
    "NOT REJECTED", "Not denied.", "The chapter is not rejected.",
    "The king's offer is rejected, as the outline says.", "Great. Reject nothing.",
])
def test_a_denied_or_story_rejection_is_no_verdict(reply):
    assert _parse_verdict_soft(reply)[0] is True


# REL-6 #61: praise words read as an exception.
@pytest.mark.parametrize("reply", [
    "Approved. Beyond reproach.", "APPROVED — beyond a doubt the strongest chapter.",
    "Approved, beyond my expectations.", "Approved. Besides being consistent, it reads beautifully.",
])
def test_beyond_and_besides_in_praise_approve(reply):
    assert _parse_verdict_soft(reply) == (True, "", False)


@pytest.mark.parametrize("reply", [
    "APPROVED, beyond the date the chapter is fine.", "APPROVED, besides the typo in line 3.",
])
def test_beyond_and_besides_naming_an_exception_reject(reply):
    assert _parse_verdict_soft(reply)[0] is False


# REL-6 #49: the "No <x> found / needed" shape, not a closed noun list.
@pytest.mark.parametrize("reply", [
    "No rework needed.", "No faults found.", "No hallucinations found.", "No red flags.",
    "No timeline slips were found.",
])
def test_no_x_found_approves(reply):
    assert _parse_verdict_soft(reply) == (True, "", False)


@pytest.mark.parametrize("reply", [
    "No faults found, but the date is wrong.", "No time, errors remain", "No. Errors found.",
])
def test_no_x_found_with_a_complaint_rejects(reply):
    assert _parse_verdict_soft(reply)[0] is False


# REL-1: natural NOT / NOTHING approvals.
@pytest.mark.parametrize("reply", [
    "Nothing jumps out as a problem.", "Nothing to worry about.",
    "Not obviously contradictory.", "Not at all problematic.",
])
def test_calm_denials_are_not_rejections(reply):
    assert _parse_verdict_soft(reply)[0] is True


@pytest.mark.parametrize("reply", [
    "Nothing to worry about, but the date is wrong.", "Not at all good.",
])
def test_calm_denials_with_a_complaint_still_reject(reply):
    assert _parse_verdict_soft(reply)[0] is False


@pytest.mark.parametrize("text", [
    "x" + " " * 20000 + ".", "It " * 20000 + "rejected", "The chapter. " * 5000, "NO " * 10000 + "found",
])
def test_the_buried_scan_is_linear(text):
    start = time.monotonic()
    _parse_verdict_soft(text)
    assert time.monotonic() - start < 2.0
