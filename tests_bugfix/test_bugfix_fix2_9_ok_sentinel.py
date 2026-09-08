"""tests_bugfix/test_bugfix_fix2_9_ok_sentinel.py

FIX-2 #9 — the SummaryFidelityVerifier's "no correction needed" guard was:

    first_line = reply.splitlines()[0].strip().upper()
    if first_line.startswith("OK") and len(reply) <= 4:

which accepts any reply that both starts with "OK" and is at most four
characters long. Two independent failure modes fall out of that:

1. ``"OKAY"`` starts with "OK" and is exactly four characters — a real
   English word a model can emit instead of the bare "OK" sentinel the
   system prompt asks for, and it gets treated as "no correction needed"
   even though it was never the sentinel.
2. A short two-line reply such as ``"OK\\nX"`` is also four characters
   total and passes the length check, silently discarding whatever the
   model put on the second line — this is the shape several of the
   candidate one-line fixes (tightening the length bound but keeping a
   first-line-only check) still let through.

The fix replaces the loose guard with ``_is_ok_sentinel()``, which requires
the *entire* reply (stripped, case-insensitive) to equal "OK" exactly.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.summary_memory import _is_ok_sentinel  # noqa: E402


class TestIsOkSentinel:
    def test_bare_ok_matches(self) -> None:
        assert _is_ok_sentinel("OK") is True

    def test_lowercase_ok_matches(self) -> None:
        assert _is_ok_sentinel("ok") is True

    def test_whitespace_padded_ok_matches(self) -> None:
        assert _is_ok_sentinel("  OK  \n") is True

    def test_okay_does_not_match(self) -> None:
        """The original bug: 'OKAY' starts with 'OK' and is 4 chars long,
        so the old ``startswith("OK") and len(reply) <= 4`` guard wrongly
        accepted it."""
        assert _is_ok_sentinel("OKAY") is False

    def test_other_four_char_ok_prefixed_word_does_not_match(self) -> None:
        assert _is_ok_sentinel("OKish") is False

    def test_short_two_line_reply_does_not_match(self) -> None:
        """'OK\\nX' is 4 characters total and starts with 'OK' on its first
        line — several of the candidate fixes that only tightened the
        length bound (``len(reply) <= 4``) while still checking just the
        first line still let this through, silently dropping the second
        line's content."""
        assert _is_ok_sentinel("OK\nX") is False

    def test_ok_followed_by_real_corrections_does_not_match(self) -> None:
        """A reply that opens with 'OK' but goes on to list real
        corrections must not be treated as the sentinel — one candidate
        fix dropped the length bound entirely and matched on the first
        line alone, which would silently discard genuine corrections
        exactly like this."""
        reply = "OK\n- The character's age should be 30, not 25.\n- Fixed."
        assert _is_ok_sentinel(reply) is False

    def test_ok_with_period_does_not_match(self) -> None:
        """The sentinel is the bare word; trailing punctuation is not it."""
        assert _is_ok_sentinel("OK.") is False

    def test_empty_string_does_not_match(self) -> None:
        assert _is_ok_sentinel("") is False

    def test_bullet_list_does_not_match(self) -> None:
        assert _is_ok_sentinel("- corrected fact one\n- corrected fact two") is False
