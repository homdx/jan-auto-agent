"""tests_bugfix/test_arena_set_status_text_210.py — bug 210: `set_status_text` damages the ticket it rewrites.

`tickets.set_status_text` is what `arena issue queue|open|close|reopen` write a
ticket with, and its docstring promises "everything else byte for byte". Three ways
it did not keep that promise:

  * The `**Status:**` match was made on the text, then the old `**Closed:**` line was
    cut out of the text, and the stale match offsets were used on the shorter text. A
    ticket with `**Closed:**` ABOVE `**Status:**` came out as
    `**File**Status:** queuedy`: the end of a header line and the blank line after it
    gone, and `issue queue` committed that.
  * A CRLF ticket: `**Status:**` and `[^\\n]*` take the `\\r` into the match, the new
    line was written without it, so the status line (and a written `**Closed:**`)
    ended in a bare `\\n` in a file of `\\r\\n` — the mix bug 185 had just fixed in the
    reading of git output.
  * `_CLOSED_LINE_RE` already ends at the line's `\\n`; the code then took one more
    `\\n` — the blank line after the header — so `closed` then `open` did not give the
    ticket back.
A status line's indentation was dropped too.
"""

from __future__ import annotations

import random
import re

import pytest

from tools.arena import tickets

HEAD_LF = "# T\n\n**File:** a.py\n**Symbol:** f\n**Status:** open\n\n## Body\ntext\n"


def _crlf(text: str) -> str:
    return text.replace("\n", "\r\n")


def test_a_closed_line_above_the_status_line_does_not_shift_the_rewrite():
    text = "# T\n\n**Closed:** old\n**Status:** closed\n**File:** a.py\n\n## Body\n"
    assert tickets.set_status_text(text, "open") == "# T\n\n**Status:** open\n**File:** a.py\n\n## Body\n"
    assert tickets.set_status_text(text, "queued", "judged") == (
        "# T\n\n**Status:** queued (judged)\n**File:** a.py\n\n## Body\n")


def test_a_closed_line_above_the_status_line_is_replaced_not_duplicated():
    text = "# T\n\n**Closed:** old\n**Status:** closed\n\n## Body\n"
    out = tickets.set_status_text(text, "closed", reason="new")
    assert out.count("**Closed:**") == 1
    assert tickets.closed_reason(out) == "new" and tickets.status_word(out) == "closed"
    assert out.endswith("\n\n## Body\n")


def test_a_crlf_ticket_keeps_every_line_ending():
    text = _crlf(HEAD_LF)
    out = tickets.set_status_text(text, "queued", "judged on arena")
    assert out == _crlf(HEAD_LF.replace("**Status:** open", "**Status:** queued (judged on arena)"))
    assert not re.search(r"(?<!\r)\n", out)


def test_a_closed_line_written_into_a_crlf_ticket_ends_in_crlf_too():
    out = tickets.set_status_text(_crlf(HEAD_LF), "closed", reason="parked")
    assert out == _crlf(HEAD_LF.replace("**Status:** open", "**Status:** closed\n**Closed:** parked"))
    assert not re.search(r"(?<!\r)\n", out)


@pytest.mark.parametrize("make", [lambda t: t, _crlf], ids=["lf", "crlf"])
def test_closing_and_reopening_gives_the_ticket_back(make):
    text = make(HEAD_LF)
    closed = tickets.set_status_text(text, "closed", reason="why")
    assert closed != text and tickets.closed_reason(closed) == "why"
    assert tickets.set_status_text(closed, "open") == text


def test_reopening_keeps_the_blank_line_after_the_header():
    closed = "# T\n\n**Status:** closed\n**Closed:** why\n\n## Body\n"
    assert tickets.set_status_text(closed, "open") == "# T\n\n**Status:** open\n\n## Body\n"


def test_the_indentation_of_the_status_line_stays():
    assert tickets.set_status_text("# T\n\n  **Status:** open\n", "queued") == "# T\n\n  **Status:** queued\n"


def test_a_ticket_with_no_status_line_is_still_refused():
    with pytest.raises(ValueError, match="no \\*\\*Status:\\*\\* line"):
        tickets.set_status_text("# T\n\n**File:** a.py\n", "open")


def _other_lines(text: str) -> str:
    return "".join(line for line in text.splitlines(keepends=True)
                   if not re.match(r"^[ \t]*\*\*(Status|Closed):\*\*", line))


def _random_ticket(rng: random.Random) -> tuple[str, str]:
    eol = rng.choice(["\n", "\r\n"])
    fields = ["**File:** a.py", "**Symbol:** f", "**Status:** open"]
    if rng.random() < 0.5:
        fields.append("**Closed:** old reason")
    rng.shuffle(fields)
    lines = ["# Title", ""] + fields + [""] * rng.randint(0, 1) + ["## Body", "text", "", "## More", "x"]
    return eol.join(lines) + eol * rng.randint(0, 1), eol


def test_whatever_the_layout_only_the_status_and_closed_lines_change():
    rng = random.Random(210)
    for _ in range(300):
        text, eol = _random_ticket(rng)
        for word, note, reason in (("queued", "", ""), ("closed", "", "done"), ("open", "", ""),
                                   ("queued", "judged on arena", "")):
            out = tickets.set_status_text(text, word, note, reason)
            assert tickets.status_word(out) == word, (text, out)
            assert tickets.closed_reason(out) == (reason if word == "closed" else ""), (text, out)
            assert tickets.status_note(out) == note, (text, out)
            assert _other_lines(out) == _other_lines(text), (text, out)
            if eol == "\r\n":
                assert not re.search(r"(?<!\r)\n", out), (text, out)
