"""FIX-2 #10 — ThemeVerdict.feedback() reported "approved" on approval.

``ThemeVerdict`` documents itself as "same shape as ContinuityVerdict", but
its ``feedback()`` returned ``self.reason or ("approved" if self.approved
else "revise")`` while ``ContinuityVerdict.feedback()`` returns "" when
approved and the verbatim instruction otherwise. Two concrete consequences:

* a caller cannot use "non-empty feedback" as "there is a problem", because
  a passing theme check answered with the word "approved";
* the fail-open notes ``check()`` puts in ``reason`` on an approved verdict
  ("no guidelines configured", "llm error — passed on fail-open") were
  surfaced as if they were coder-facing revision instructions.

The fix makes the two implementations identical. ``reason`` is still
retained on the verdict for logging — only ``feedback()`` changes.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.continuity_validator import ContinuityVerdict  # noqa: E402
from tools.auto.theme_validator import ThemeVerdict  # noqa: E402


class TestApprovedVerdictIsSilent:
    def test_approved_without_reason_gives_empty_feedback(self) -> None:
        assert ThemeVerdict(approved=True).feedback() == ""

    def test_approved_with_a_failopen_note_gives_empty_feedback(self) -> None:
        """The note stays on the verdict for logging, but is not coder-facing."""
        v = ThemeVerdict(approved=True, reason="llm error — passed on fail-open")
        assert v.feedback() == ""
        assert v.reason == "llm error — passed on fail-open"

    def test_no_guidelines_note_is_not_surfaced(self) -> None:
        assert ThemeVerdict(approved=True, reason="no guidelines configured").feedback() == ""

    def test_unparseable_failopen_verdict_is_silent(self) -> None:
        v = ThemeVerdict(approved=True, reason="rambling reply", unparseable=True)
        assert v.feedback() == ""


class TestRejectionCarriesTheInstructionVerbatim:
    def test_reason_is_returned_unchanged(self) -> None:
        reason = "remove the modern slang from chapter 3"
        assert ThemeVerdict(approved=False, reason=reason).feedback() == reason

    def test_reason_is_not_reworded_or_prefixed(self) -> None:
        v = ThemeVerdict(approved=False, reason="drop the profanity")
        assert v.feedback() == "drop the profanity"
        assert "revise" not in v.feedback()


class TestMirrorsContinuityVerdict:
    """The class docstring promises the same shape; pin it so the two cannot
    drift apart again."""

    def test_same_feedback_for_every_combination(self) -> None:
        for approved in (True, False):
            for reason in ("", "some concrete instruction"):
                theme = ThemeVerdict(approved=approved, reason=reason).feedback()
                cont = ContinuityVerdict(approved=approved, reason=reason).feedback()
                assert theme == cont, (approved, reason)
