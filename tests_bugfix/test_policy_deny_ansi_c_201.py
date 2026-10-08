r"""tests_bugfix/test_policy_deny_ansi_c_201.py — regression test for bug 12 of 201.

Before this fix the ``deny_commands`` reader read ``$'…'`` (ANSI-C quoting) as a
plain ``'`` quote. A backslash does not close an ANSI-C string, so in
``echo $'a\'b'; git push`` the reader ended the string at ``\'``, opened a new
quote that ran to the end of the line and hid the ``; git push`` — a denied
command passed. ``bash -c $'git push'`` and ``eval $'git push'`` went the other
way: the body was unquoted as if the ``$'`` were not there, so it read as the
literal ``$'git push'`` and never matched ``git push*``.

The reader now walks ``$'…'`` like the shell: ``\`` escapes the next character,
the string ends at the first unescaped ``'``, and the ``bash -c`` / ``eval``
unquoting decodes the escapes (``\n``, ``\t``, ``\\``, ``\'``, ``\"``, ``\xHH``,
``\NNN``) before it reads the body as a command line. Fail open: a string that
never closes, or an escape that cannot be decoded, is kept as written rather
than raising into a run.
"""

import pytest

from tools.contest.policy import _deny_match

DENY = ["git push*"]

# (command, expected pattern or None)
CASES = [
    # a \' is a quote character inside $'…', not the end of the string
    (r"echo $'a\'b'; git push", "git push*"),
    (r"echo $'\''; git push", "git push*"),
    # \\ is a backslash: the next ' closes the string, so the push is live
    (r"echo $'a\\'; git push", "git push*"),
    # the reader already handled these; they must stay
    ("echo $'x'; git push", "git push*"),
    ("echo $'a;b' ; git push", "git push*"),
    ('echo $"a"; git push', "git push*"),

    # a ; that is inside the string is data, whatever the string is quoted with
    ("echo $'a; git push'", None),
    (r"echo $'a\'; git push'", None),
    ("echo $'git push'", None),

    # bash -c and eval get the decoded body, which is the command that runs
    ("bash -c $'git push'", "git push*"),
    ("eval $'git push'", "git push*"),
    ("bash -c $'git push --force'", "git push*"),
    ("bash -c $'a; git push'", "git push*"),
    ("bash -c $'echo hi'", None),
    ("bash -c $'echo hi'; git push", "git push*"),
    ("eval $'echo hi'", None),

    # the escapes a body may be written with
    (r"bash -c $'git \160ush'", "git push*"),
    (r"bash -c $'\x67it push'", "git push*"),
    (r"bash -c $'gi\164 push'", "git push*"),
    (r"bash -c $'git push\t--force'", "git push*"),

    # fail open: an unclosed string hides nothing it could hide, and no error
    ("echo $'a; git push", None),
    ("bash -c $'git push", None),
]


@pytest.mark.parametrize("command,expected", CASES, ids=range(len(CASES)))
def test_deny_match_ansi_c_201(command, expected):
    got = _deny_match(command, DENY)
    if expected is None:
        assert got is None, f"expected no match, got {got!r} for {command!r}"
    else:
        assert got is not None, f"expected match, got None for {command!r}"
