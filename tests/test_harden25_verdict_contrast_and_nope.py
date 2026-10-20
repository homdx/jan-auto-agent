"""HARDEN-25 (own finds): a contrast after a conflict denial rejects, `in it` is a place, a `Nope,` clause names its problem without a noun."""
import pytest

from tools.auto.inner_loop import _parse_verdict_soft


def _verdict(reply):
    approved, reason, unparseable = _parse_verdict_soft(reply)
    return approved, reason, unparseable


@pytest.mark.parametrize("reply", [
    "Nothing in it breaks the story.",
    "Nothing in them contradicts the outline.",
    "Nothing here contradicts the outline!",
    "No, nothing here contradicts the outline.",
    "Nothing here contradicts the outline. APPROVED",
])
def test_a_conflict_denial_approves_with_an_empty_reason(reply):
    assert _verdict(reply) == (True, "", False)


@pytest.mark.parametrize("reply", [
    "Nothing here contradicts the outline but the ending fails.",
    "Nothing here contradicts the outline except the date.",
    "Nothing in the plan breaks the timeline however the date is wrong.",
    "Nothing here contradicts the outline though the ring is stolen.",
])
def test_a_contrast_after_the_object_of_a_denial_rejects_with_the_reply_as_the_reason(reply):
    approved, reason, unparseable = _verdict(reply)
    assert approved is False and unparseable is False and reason == reply


@pytest.mark.parametrize("reply", [
    "Nope, this contradicts the outline.",
    "Nope, there is an error in chapter two.",
    "Nope, not right.",
    "Nope, that is not correct.",
    "Nope, it violates the timeline.",
])
def test_a_nope_clause_names_its_problem_without_a_noun(reply):
    approved, reason, unparseable = _verdict(reply)
    assert approved is False and unparseable is False and reason


@pytest.mark.parametrize("reply", [
    "Nope, not bad",
    "Nope, no problem at all",
    "Nope, nothing wrong",
    "Nope, works",
    "Nope, looks fine",
])
def test_a_nope_clause_that_denies_the_problem_stays_prose(reply):
    assert _verdict(reply)[0] is True
