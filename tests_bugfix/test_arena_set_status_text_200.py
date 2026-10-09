"""tests_bugfix/test_arena_set_status_text_200.py — Bug: `tickets.set_status_text` lost a line
of the ticket it rewrote, ate a blank line on `close` then `reopen`, and gave a CRLF ticket
mixed line endings.

Field report, bug 12 of round 200: the status line was found once, in the original text, and
then the `**Closed:**` line was cut out of `text` with those same offsets applied to the
shortened string. On `# T⏎**Closed:** why⏎**Status:** closed⏎**File:** f⏎` a `reopen` wrote
`# T⏎**Status:** clos**Status:** open` — the `**File:**` line, gone, and nobody told.

Bug 13: `_CLOSED_LINE_RE` already swallowed a newline, so the removal code took a second one
and ate the blank line after `**Closed:**`; `close` then `reopen` no longer gave the ticket
back byte for byte. Bug 14: the new status line went out without the original `\r` and
`**Closed:**` went out with a bare `\n`, so a `\\r\\n` ticket came back mixed.

The fix is one pass over the lines: the status line and the `**Closed:**` line are found in
the same text, the new lines take the status line's own ending, and the old `**Closed:**`
line goes with its own ending and nothing else. These tests pin all three, and the round trip
that is supposed to be an identity.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.arena import tickets


# A ticket that may hold a `**Closed:**` line above the `**Status:**` line: the shape that
# cut the wrong text.
CLOSED_ABOVE = "# T\n**Closed:** superseded\n**Status:** closed\n**File:** pkg/a.py\n"

#: The tickets `close` then `reopen` must return unchanged — with or without a blank line,
#: with or without a trailing newline. A status note is out of the round trip on purpose:
#: `close` writes the word alone, so the note goes with it (see the test below).
ROUND_TRIPS = [
    "# T\n**Status:** open\n\nbody\n",
    "# T\n**Status:** open\n**File:** pkg/a.py\n",
    "# T\n**Status:** open\n",
    "# T\r\n**Status:** open\r\n\r\nbody\r\n",
    "# T\r\n**Status:** open\r\n**File:** pkg/a.py\r\n",
]


def test_a_closed_line_above_the_status_line_no_longer_cuts_the_next_line():
    """Bug 12: the removal no longer shifts the status line's offsets."""
    out = tickets.set_status_text(CLOSED_ABOVE, "open")
    assert out == "# T\n**Status:** open\n**File:** pkg/a.py\n"


@pytest.mark.parametrize("text", ROUND_TRIPS)
def test_close_then_reopen_gives_the_ticket_back_byte_for_byte(text):
    """Bug 13: the round trip is the identity, blank line and all."""
    closed = tickets.set_status_text(text, "closed", reason="no longer wanted")
    assert "**Closed:** no longer wanted" in closed
    assert tickets.set_status_text(closed, "open") == text


def test_close_then_reopen_several_times_over_is_still_the_identity():
    text = "# T\n**Status:** open\n\nbody\n"
    for _ in range(4):
        text = tickets.set_status_text(
            tickets.set_status_text(text, "closed", reason="again"), "open")
    assert text == "# T\n**Status:** open\n\nbody\n"


def test_close_then_reopen_of_a_crlf_ticket_is_the_identity():
    """Bug 14: a `\\r\\n` ticket stays `\\r\\n` all the way through."""
    text = "# T\r\n**Status:** open\r\n**File:** pkg/a.py\r\n"
    closed = tickets.set_status_text(text, "closed", reason="r")
    assert closed == "# T\r\n**Status:** closed\r\n**Closed:** r\r\n**File:** pkg/a.py\r\n"
    assert "\n" not in closed.replace("\r\n", "")
    assert tickets.set_status_text(closed, "open") == text


def test_the_closed_line_goes_directly_under_the_status_line_and_nothing_else_moves():
    text = "# T\n\n**Status:** open\n**Severity:** LOW\n\nbody\n"
    out = tickets.set_status_text(text, "closed", reason="dup of 7")
    assert out == "# T\n\n**Status:** closed\n**Closed:** dup of 7\n**Severity:** LOW\n\nbody\n"


def test_a_ticket_with_no_closed_line_is_untouched_but_for_the_status_line():
    text = "# T\n**Status:** open\n\nbody\n"
    out = tickets.set_status_text(text, "queued", note="judged on arena")
    assert out == "# T\n**Status:** queued (judged on arena)\n\nbody\n"


def test_a_reason_is_stripped_of_its_trailing_space():
    out = tickets.set_status_text("# T\n**Status:** open\n", "closed", reason="  superseded  \n")
    assert "**Closed:** superseded\n" in out


def test_reopen_after_a_note_keeps_the_note_out_and_moves_nothing_else():
    """The status note is the note's own business: `close` drops it, nothing else moves."""
    text = "# T\n**Status:** closed (parked for a release)\n\nbody\n"
    closed = tickets.set_status_text(text, "closed", reason="r")
    assert closed == "# T\n**Status:** closed\n**Closed:** r\n\nbody\n"
    assert tickets.set_status_text(closed, "open") == "# T\n**Status:** open\n\nbody\n"


def test_a_closed_line_that_is_the_last_line_takes_its_own_newline_and_only_that():
    out = tickets.set_status_text("# T\n**Status:** closed\n**Closed:** r", "open")
    assert out == "# T\n**Status:** open\n"
    assert "**Closed:**" not in out


def test_a_status_line_with_no_newline_of_its_own_gets_one_under_the_closed_line():
    out = tickets.set_status_text("# T\n**Status:** open", "closed", reason="r")
    assert out == "# T\n**Status:** closed\n**Closed:** r"


def test_closing_a_ticket_that_is_already_closed_keeps_one_closed_line():
    text = "# T\n**Status:** closed\n**Closed:** first\n**File:** f\n"
    out = tickets.set_status_text(text, "closed", reason="second")
    assert out.count("**Closed:**") == 1
    assert out == "# T\n**Status:** closed\n**Closed:** second\n**File:** f\n"


def test_a_status_word_that_already_carries_its_note_is_written_as_given():
    """`issue land` passes the whole line after `**Status:**` as *word*."""
    out = tickets.set_status_text("# T\n**Status:** open\n", "landed — round 7")
    assert out == "# T\n**Status:** landed — round 7\n"


def test_a_ticket_with_no_status_line_still_raises_value_error():
    with pytest.raises(ValueError):
        tickets.set_status_text("# T\n\nbody\n", "open")


def test_closed_reason_still_reads_a_crlf_ticket():
    assert tickets.closed_reason(CLOSED_ABOVE.replace("\n", "\r\n")) == "superseded"
