"""tests/test_auto_verdict_boundary.py — HARDEN-1 A1: the Gate-2 verdict tokens
match as whole words.

The creative-mode verdict parser read ``startswith("NO")``, so any approval
whose first word merely *began* with a verdict token became a rejection whose
"reason" was the word's own tail, and that garbage was handed to the Coder as
the thing to fix — a round burned on nothing:

    _parse_verdict_soft("NOT BAD")                    -> (False, 'T BAD', False)
    _parse_verdict_soft("Nothing to add, looks good") -> (False, 'thing to add, looks good', False)
    _parse_verdict_soft("NOTE: fine")                 -> (False, 'TE: fine', False)
    _parse_verdict_soft("Normally fine")              -> (False, 'rmally fine', False)
    _parse_verdict_soft("NOPE")                       -> (False, 'PE', False)

``REVISED`` and ``REJECTION`` were read the same way. A verdict token now ends
at a non-alphanumeric character or at the end of the line, so ``NO: broken``,
``No - broken`` and a bare ``NO`` still reject and prose falls through to the
existing fail-open rules.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.inner_loop import _parse_verdict_soft  # noqa: E402


class TestAWordThatBeginsWithAVerdictTokenIsNotAVerdict:
    """The five replies the ticket reproduced, and their siblings."""

    def test_not_bad_is_not_a_rejection(self):
        approved, _reason, unparseable = _parse_verdict_soft("NOT BAD")
        assert approved is True and unparseable is True

    def test_nothing_to_add_is_not_a_rejection(self):
        approved, _reason, unparseable = _parse_verdict_soft("Nothing to add, looks good")
        assert approved is True and unparseable is True

    def test_note_is_not_a_rejection(self):
        approved, _reason, unparseable = _parse_verdict_soft("NOTE: fine")
        assert approved is True and unparseable is True

    def test_normally_is_not_a_rejection(self):
        approved, _reason, unparseable = _parse_verdict_soft("Normally fine")
        assert approved is True and unparseable is True

    def test_nope_is_not_a_rejection(self):
        approved, _reason, unparseable = _parse_verdict_soft("NOPE")
        assert approved is True and unparseable is True

    def test_the_reason_is_never_the_words_own_tail(self):
        for reply in ("NOT BAD", "Nothing to add, looks good", "NOTE: fine",
                      "Normally fine", "NOPE"):
            _approved, reason, _unparseable = _parse_verdict_soft(reply)
            assert reason != "T BAD"
            assert not reason.startswith(("T BAD", "thing to", "TE:", "rmally", "PE"))

    def test_revised_and_rejection_are_not_verdicts(self):
        for reply in ("REVISED the draft", "REJECTION is not warranted"):
            approved, _reason, unparseable = _parse_verdict_soft(reply)
            assert approved is True and unparseable is True


class TestAVerdictTokenStillRejects:
    """The boundary must not cost the verdicts it was written for."""

    def test_no_with_a_colon_still_rejects(self):
        approved, reason, unparseable = _parse_verdict_soft("NO: broken")
        assert (approved, reason, unparseable) == (False, "broken", False)

    def test_no_with_a_comma_still_rejects(self):
        approved, reason, unparseable = _parse_verdict_soft("NO, broken")
        assert approved is False and unparseable is False and "broken" in reason

    def test_no_with_a_dash_still_rejects(self):
        approved, reason, unparseable = _parse_verdict_soft("No - x")
        assert approved is False and unparseable is False and "x" in reason

    def test_a_bare_no_still_rejects(self):
        approved, _reason, unparseable = _parse_verdict_soft("NO")
        assert (approved, unparseable) == (False, False)

    def test_revise_and_reject_still_reject(self):
        assert _parse_verdict_soft("REVISE: x")[0] is False
        assert _parse_verdict_soft("REJECT: y")[0] is False
        assert _parse_verdict_soft("REVISE")[0] is False


class TestAVerdictOnALaterLine:
    """The per-line pass goes through the same boundary as the first line."""

    def test_a_no_verdict_on_a_later_line_still_rejects(self):
        approved, reason, unparseable = _parse_verdict_soft("I read the chapter.\nNO: broken")
        assert (approved, reason, unparseable) == (False, "broken", False)

    def test_prose_beginning_with_no_on_a_later_line_does_not_reject(self):
        approved, _reason, unparseable = _parse_verdict_soft("I read the chapter.\nNOT BAD at all")
        assert approved is True and unparseable is True

    def test_an_approval_on_a_later_line_still_approves(self):
        assert _parse_verdict_soft("I read the chapter.\nAPPROVED")[0] is True
