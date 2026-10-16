"""Review q: "No <problem>" is an approval only when it is the whole verdict."""

from __future__ import annotations

import pytest

from tools.auto.inner_loop import _parse_verdict_soft


@pytest.mark.parametrize("reply,reason", [
    ("No contradictions found, but Anna's age changes from 30 to 25 in this chapter.",
     "No contradictions found, but Anna's age changes from 30 to 25 in this chapter."),
    ("No errors except one: Ivan dies in chapter 2 and speaks here.",
     "No errors except one: Ivan dies in chapter 2 and speaks here."),
    ("No issues with tone; one problem: the ending contradicts chapter 4.",
     "No issues with tone; one problem: the ending contradicts chapter 4."),
    ("No contradictions with the bible. However, the timeline breaks.",
     "No contradictions with the bible. However, the timeline breaks."),
    ("No issues.\nREVISE: the date of the wedding is wrong.", "the date of the wedding is wrong."),
    ("No contradictions found.\nREJECT: Anna is 30, not 25.", "Anna is 30, not 25."),
    ("Checked.\nNo issues, but the ending is rushed.", "No issues, but the ending is rushed."),
    ("No, it's good, but the ending is rushed.", "No, it's good, but the ending is rushed."),
])
def test_a_no_problem_reply_that_goes_on_to_a_problem_is_a_rejection(reply, reason):
    assert _parse_verdict_soft(reply)[:2] == (False, reason)


@pytest.mark.parametrize("reply", [
    "No contradictions found.",
    "No contradictions with the story bible.",
    "No inconsistencies detected in chapter 3.",
    "No problems found.",
    "No problem!",
    "No errors.\nThe chapter keeps every fact of the bible.",
    "Checked the chapter.\nNo contradictions found.",
    "No, it's perfect.",
    "No, everything is fine",
])
def test_a_no_problem_reply_that_is_the_whole_verdict_is_an_approval(reply):
    assert _parse_verdict_soft(reply)[:2] == (True, "")
