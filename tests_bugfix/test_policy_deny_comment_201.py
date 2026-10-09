"""tests_bugfix/test_policy_deny_comment_201.py — regression test for bug 14 of 201.

Before this fix a ``#`` anywhere on the line was read, so ``echo a # <<EOF``
declared a heredoc from inside a comment. The reader then took the next line as
the heredoc body, never found the delimiter, fell back to "unterminated: search
the rest as commands", and read the ``git push`` as data — a denied command
passed. With no heredoc swallowed, ``echo a # ; git push`` was refused instead,
a ``;`` the shell treats as comment text.

``#`` starts a comment only where a word would start: after whitespace or after
`;`, `|`, `&` or `(`. Inside a word (``a#b``, ``$#``, ``${#x}``, ``$(...)#``)
and inside a quoted string it is data, and nothing there is a heredoc either.
"""

import pytest

from tools.contest.policy import _deny_match

DENY = ["git push*"]

# (command, expected pattern or None)
CASES = [
    # a # that starts a word opens a comment: no heredoc, no separator inside
    ("echo a # <<EOF\ngit push", "git push*"),
    ("echo a #<<EOF\ngit push", "git push*"),
    ("echo a # <<EOF\ngit push\nEOF", "git push*"),
    ("echo a;# x\ngit push", "git push*"),
    ("echo a;#<<EOF\ngit push", "git push*"),
    ("echo a |# x\ngit push", "git push*"),
    ("echo a &&# x\ngit push", "git push*"),
    ("(# x\ngit push)", "git push*"),
    ("echo hi\n# <<EOF\ngit push", "git push*"),
    # the comment runs to the end of the line, so the ; and the && in it are text
    ("echo a # ; git push", None),
    ("echo a # $(git push)", None),
    ("echo a # <<EOF && git push", None),
    ("echo a # x; git push", None),
    ("echo a # x && git push", None),
    ("echo a # $(git push); echo done", None),
    ("echo a # ; git push\ngit push", "git push*"),
    # a # that is not a comment start: the heredoc is still a heredoc
    ("echo a#b; git push", "git push*"),
    ("echo $#; git push", "git push*"),
    ("echo ${#x}; git push", "git push*"),
    ("echo $# <<EOF\nx; git push\nEOF", None),
    ("echo a#b <<EOF\nx; git push\nEOF", None),
    ("echo \"#\" <<EOF\nx; git push\nEOF", None),
    ("echo $(x) # git push", None),
    # nothing to do with comments, kept as a guard
    ("echo $((1 + 2)); git push", "git push*"),
    ("echo hi$((a+1)) # <<EOF\ngit push", "git push*"),
]


@pytest.mark.parametrize("command,expected", CASES, ids=range(len(CASES)))
def test_deny_match_comment_201(command, expected):
    got = _deny_match(command, DENY)
    if expected is None:
        assert got is None, f"expected no match, got {got!r} for {command!r}"
    else:
        assert got is not None, f"expected match, got None for {command!r}"


def test_a_comment_is_not_declared_from_a_malformed_line():
    # fail open: an unterminated quote, escape, delimiter or substitution never
    # raises into a run — the reader answers, whatever it answers
    for command in (
        "echo a # ' <<EOF\ngit push",
        "echo a # $'\ngit push",
        "echo a # <<EOF\n",
        "echo a#b <<EOF\nx; git push\n",
        "cat <<$'EOF\nx; git push\nEOF",
        "cat <<\\EOF\nx; git push\nEOF",
        "cat <<EOF\n$(echo ')",
        "cat <<EOF\n`echo '",
        "echo $'a; git push",
        "bash -c $'git push",
        "",
    ):
        got = _deny_match(command, DENY)
        assert got is None or got in DENY, repr(command)
