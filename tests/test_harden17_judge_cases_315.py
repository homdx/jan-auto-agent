"""HARDEN-17 judge cases (round 315): the verdict parser over the judge's own tables of rejections and approvals."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.auto.inner_loop import _parse_verdict_soft  # noqa: E402

REJECT = [
    "No issues found, but the ring is stolen in ch 3.",
    "Looks good, but the ring is stolen.",
    "Looks great, but the pacing is off.",
    "Looks fine although Anna is dead in chapter 1.",
    "Rejected: the timeline breaks.",
    "REJECTED - Anna is dead",
    "Rejected",
    "Needs revision: Anna is dead.",
    "Needs fixing: the ring is stolen.",
    "Needs work: pacing.",
    "Failed: the ring is missing.",
    "Approved, but the date is wrong.",
    "APPROVED however chapter 2 contradicts the bible",
    "No issues.\nREVISE: scene 3",
    # the directive words of Part A on a later line, not only REVISE / REJECT
    "No issues found.\nREJECTED: the date breaks",
    "No issues found.\nNeeds revision: the date breaks",
    "The chapter is consistent.\nRejected: Anna is dead in ch 1.",
]
APPROVE = [
    "No issues found; everything matches the bible.",
    "No contradictions found. The chapter only uses facts from the bible.",
    "No issues. Yet another consistent chapter.",
    "No discrepancies found.",
    "No factual errors found.",
    "No plot issues.",
    "No continuity errors.",
    "No issues found.\nThe chapter matches the bible.",
    "Looks good.",
    "Approved",
    "NOT FAILED",
]


@pytest.mark.parametrize("reply", REJECT)
def test_a_rejecting_reply_is_not_approved(reply):
    approved, reason, unparseable = _parse_verdict_soft(reply)
    assert approved is False and unparseable is False and reason


@pytest.mark.parametrize("reply", APPROVE)
def test_a_clean_approval_stays_approved(reply):
    assert _parse_verdict_soft(reply)[0] is True


def test_a_bare_rejection_word_has_the_base_reason():
    assert _parse_verdict_soft("Rejected")[1] == "validator rejected (no reason given)"
