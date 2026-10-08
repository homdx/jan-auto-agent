"""tests_bugfix/test_policy_deny_heredoc_subst_201.py — regression test for bug 13 of 201.

Before this fix a heredoc body was read as one text everywhere: a quoted
``)`` inside a ``$( … )`` opened in an unquoted body closed the substitution
early, and the ``; git push`` that followed it was lost — a denied command
passed. The body's own text must stay data (``don't``, ``echo ')'``), but the
shell reads a ``$( … )`` or backtick opened in it as a command again, where
quotes and escapes work normally and parens must be counted.

The body is therefore read as text except inside a substitution opened in it,
where the normal shell reader applies.
"""

import pytest

from tools.contest.policy import _deny_match

DENY = ["git push*"]

# (command, expected pattern or None)
CASES = [
    # a quoted paren inside the substitution of an unquoted heredoc body
    ("cat <<EOF\n$(echo ')'; git push)\nEOF", "git push*"),
    ('cat <<EOF\n$(echo ")"; git push)\nEOF', "git push*"),
    ("cat <<EOF\n$(echo \\); git push)\nEOF", "git push*"),
    ("cat <<EOF\n" + "`echo ')'; git push`" + "\nEOF", "git push*"),
    # nested: the inner substitution is searched too
    ("cat <<EOF\n$(echo $(echo ')'; git push))\nEOF", "git push*"),
    ("cat <<EOF\n$(printf ')' ; echo $(git push))\nEOF", "git push*"),
    # the substitution may be at the end of the body or the body may be the end
    ("cat <<EOF\nx\n$(echo ')'; git push)", "git push*"),
    ("cat <<EOF\n$(echo ')'; git push)\nEOF\ngit push", "git push*"),
    # a quoted delimiter makes the body data: nothing in it runs
    ("cat <<'EOF'\n$(echo ')'; git push)\nEOF", None),
    ("cat <<'EOF'\n" + "`echo ')'; git push`" + "\nEOF", None),
    # a paren that is data in the body is not a substitution at all
    ("cat <<EOF\n$(echo ')')\nEOF", None),
    ("cat <<EOF\necho ')'\nEOF", None),
    ("cat <<EOF\necho ')' ; git push\nEOF", None),
]


@pytest.mark.parametrize("command,expected", CASES, ids=range(len(CASES)))
def test_deny_match_heredoc_subst_201(command, expected):
    got = _deny_match(command, DENY)
    if expected is None:
        assert got is None, f"expected no match, got {got!r} for {command!r}"
    else:
        assert got is not None, f"expected match, got None for {command!r}"
