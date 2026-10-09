r"""tests_bugfix/test_policy_deny_quoted_delimiter_201.py — regression test for bug 15 of 201.

Before this fix a heredoc's delimiter word was compared as written, and the
body was treated as data only when the whole word was quoted. The shell
quote-removes the word (``\EOF``, ``E"O"F``, ``'E'OF``, ``$'EOF'`` all have the
delimiter ``EOF``) and expands the body only when *no part* of the word was
quoted. So ``cat <<E"O"F`` was read with the delimiter ``E"O"F``, the body was
never terminated, the reader fell back to "unterminated: search the rest as
commands" and refused a body the shell would only print.

The delimiter word is now quote-removed and the heredoc is marked quoted when
any piece of it was quoted.
"""

import pytest

from tools.contest.policy import _deny_match

DENY = ["git push*"]

BODIES = ["x; git push\n", "x\n"]
DELIMITERS = (
    # every spelling the shell quote-removes to the delimiter EOF and reads as data
    "\\EOF",
    'E"O"F',
    "'E'OF",
    '"EOF"',
    "$'EOF'",
)

# (command, expected pattern or None)
CASES = []
for delimiter in DELIMITERS:
    # a body that is data: the ; and the $(…) in it are text, not a command
    CASES.append((f"cat <<{delimiter}\n" + BODIES[0] + "EOF", None))
    # the command that runs after the heredoc is still a command
    CASES.append((f"cat <<{delimiter}\n" + BODIES[1] + "EOF\ngit push", "git push*"))
    # a $(…) in a data body is not run
    CASES.append((f"cat <<{delimiter}\n$(git push)\nEOF", None))
CASES += [
    # the unquoted heredoc: the same bodies really do run their substitutions
    ("cat <<EOF\n" + BODIES[0] + "EOF", None),
    ("cat <<EOF\n$(git push)\nEOF", "git push*"),
    ("cat <<EOF\n" + "`git push`" + "\nEOF", "git push*"),
    ("cat <<EOF\n" + BODIES[1] + "EOF\ngit push", "git push*"),
    # a quoted delimiter in full, unchanged
    ("cat <<'EOF'\n$(git push)\nEOF", None),
    ("cat <<'EOF'\n" + BODIES[1] + "EOF\ngit push", "git push*"),
    # the dash form is unaffected
    ("cat <<-EOF\n\tx; git push\n\tEOF", None),
    ("cat <<-EOF\n\tx\n\tEOF\ngit push", "git push*"),
    # two heredocs in a row, the second quoted piece by piece
    ('cat <<A <<B\nx; git push\nA\ny; git push\nB', None),
    # the push that runs after a data body is still a push
    ("cat <<\\EOF\nx\nEOF\ngit push", "git push*"),
    ("cat <<E\"O\"F\nx\nEOF\ngit push", "git push*"),
    ("cat <<$'EOF'\nx\nEOF\ngit push", "git push*"),
]


@pytest.mark.parametrize("command,expected", CASES, ids=range(len(CASES)))
def test_deny_match_quoted_delimiter_201(command, expected):
    got = _deny_match(command, DENY)
    if expected is None:
        assert got is None, f"expected no match, got {got!r} for {command!r}"
    else:
        assert got is not None, f"expected match, got None for {command!r}"
