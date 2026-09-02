"""tests/test_bugfix_inner_loop_english_no_discourse_filler.py

Bug: `_parse_verdict_soft`'s English verdict check used a bare
`upper.startswith("NO")`, so a casual approving reply that happens to open
with "No" as discourse filler — "No, it's perfect!", "No, that's fine." —
was misread as a REVISE verdict, with the reason mangled to ", it's
perfect!". The Russian branch already had an equivalent guard for the
identical discourse pattern ("Нет, всё хорошо" correctly treated as
approval); this fix mirrors it for English. Genuine "No"-led rejections
that actually name a problem must remain unaffected.
"""
from tools.auto.inner_loop import _parse_verdict_soft


def test_no_its_perfect_is_approved():
    approved, reason, unparseable = _parse_verdict_soft("No, it's perfect!")
    assert approved is True
    assert unparseable is False


def test_no_thats_fine_is_approved():
    approved, reason, unparseable = _parse_verdict_soft("No, that's fine.")
    assert approved is True


def test_no_all_good_is_approved():
    approved, reason, unparseable = _parse_verdict_soft("No, all good.")
    assert approved is True


def test_genuine_no_rejection_with_problem_is_not_approved():
    approved, reason, unparseable = _parse_verdict_soft("No, this does not work.")
    assert approved is False


def test_genuine_no_rejection_with_instruction_is_not_approved():
    approved, reason, unparseable = _parse_verdict_soft("No, fix the ending.")
    assert approved is False


def test_explicit_revise_protocol_token_still_works():
    approved, reason, unparseable = _parse_verdict_soft("REVISE: fix the ending")
    assert approved is False
    assert reason == "fix the ending"


def test_approved_protocol_token_still_works():
    approved, reason, unparseable = _parse_verdict_soft("APPROVED")
    assert approved is True
