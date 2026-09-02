"""tests/test_verdict_soft_english_multiline.py

Bug found during a cold-read audit of tools/auto/inner_loop._parse_verdict_soft.

_parse_verdict_soft builds a two-element candidate list —
[first_non_empty_line, whole_text] — specifically so a verdict that does NOT
sit on the first line can still be recovered via the whole-text fallback.
That fallback worked for RUSSIAN verdicts (the Russian rules use substring
``.search()`` over the whole normalised text) but NOT for English ones: the
English APPROVED / REVISE / REJECT / NO detection used ``str.startswith`` on
the candidate as a whole, so on the whole-text candidate it only matched when
the FIRST line was the verdict — exactly the case the first-line candidate
already covered. Net effect: the whole-text fallback added nothing for
English.

Concrete live consequence: a Gate-2 / continuity / theme validator that
replies with a clear English "REVISE: <fix>" (or REJECT / NO) but prepends any
preamble line ("Here is my verdict:\nREVISE: ...") had its rejection SILENTLY
DROPPED — the parser returned unparseable=True, which fail-opens to
approved=True — while the byte-for-byte-equivalent Russian reply was correctly
classified as a rejection. Real 7B/8B and thinking models routinely add a
preamble line despite the one-line instruction, so this defeated the gate for
English-language stories.

Fix: scan each LINE for a leading English verdict token (the protocol is
"first token of a line"), placed after the existing startswith checks so every
input that already returned a definite verdict is unchanged.

Each EXPECT_FIXED test below FAILS on unfixed code (returns approved=True /
unparseable=True) and PASSES after the fix. The regression tests guard the
behaviour that must NOT change.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.auto.inner_loop import _parse_verdict_soft  # noqa: E402


class TestEnglishVerdictBehindPreamble:
    """These all FAIL on unfixed code — the bug being fixed."""

    def test_revise_on_second_line_is_a_rejection(self):
        approved, reason, unparseable = _parse_verdict_soft(
            "Here is my verdict:\nREVISE: fix the ending"
        )
        assert approved is False
        assert unparseable is False
        assert reason == "fix the ending"

    def test_reject_on_second_line_is_a_rejection(self):
        approved, _reason, unparseable = _parse_verdict_soft(
            "My assessment of the chapter:\nREJECT: contradicts chapter 2"
        )
        assert approved is False
        assert unparseable is False

    def test_no_on_second_line_is_a_rejection(self):
        approved, _reason, unparseable = _parse_verdict_soft(
            "Verdict below.\nNO: the tone is wrong here"
        )
        assert approved is False
        assert unparseable is False

    def test_reason_is_extracted_from_the_verdict_line_not_the_preamble(self):
        _approved, reason, _unparseable = _parse_verdict_soft(
            "Thoughts: the pacing is fine overall.\nREVISE: cut the flashback"
        )
        assert reason == "cut the flashback"


class TestEnglishApprovedBehindPreambleStillApproves:
    """A real APPROVED behind a preamble should be a clean (parseable)
    approval, not a fail-open one."""

    def test_approved_on_second_line_is_clean_approval(self):
        approved, _reason, unparseable = _parse_verdict_soft(
            "Let me think about this.\nAPPROVED"
        )
        assert approved is True
        assert unparseable is False  # parseable, NOT fail-open

    def test_ok_on_second_line_is_clean_approval(self):
        approved, _reason, unparseable = _parse_verdict_soft(
            "Analysis complete.\nOK looks good to me"
        )
        assert approved is True
        assert unparseable is False


class TestNoRegressionOnExistingBehaviour:
    """Behaviour that must remain byte-for-byte identical after the fix."""

    def test_plain_first_line_revise_unchanged(self):
        approved, reason, unparseable = _parse_verdict_soft("REVISE: fix the ending")
        assert (approved, reason, unparseable) == (False, "fix the ending", False)

    def test_plain_approved_unchanged(self):
        assert _parse_verdict_soft("APPROVED") == (True, "", False)

    def test_genuinely_unparseable_still_fails_open(self):
        approved, _reason, unparseable = _parse_verdict_soft(
            "The chapter is interesting and generally well written."
        )
        assert approved is True
        assert unparseable is True  # fail-open preserved for real rambling

    def test_russian_revise_behind_preamble_still_rejects(self):
        approved, _reason, unparseable = _parse_verdict_soft(
            "Мой разбор главы:\nнужно исправить финал"
        )
        assert approved is False
        assert unparseable is False

    def test_russian_approved_unchanged(self):
        approved, _reason, unparseable = _parse_verdict_soft("нет противоречий")
        assert approved is True
        assert unparseable is False

    def test_english_verdict_behind_neutral_russian_preamble(self):
        # A neutral (non-verdict) Russian preamble line followed by an explicit
        # English REVISE is correctly rejected — the preamble carries no
        # approval/rejection keyword, so the whole-text line scan finds REVISE.
        approved, reason, unparseable = _parse_verdict_soft(
            "Мой вердикт по главе:\nREVISE: перепиши концовку"
        )
        assert approved is False
        assert unparseable is False
        assert reason == "перепиши концовку"
