"""Ticket 213: a quote in an unquoted heredoc body no longer hides a `$(…)` from deny_commands."""
import pytest

from tools.contest.policy import _deny_match

DENY = ["git push*"]


@pytest.mark.parametrize("command, expected", [
    # the ticket's four lines
    ("cat <<EOF\ndon't\nEOF$(git push)", "git push*"),
    ("cat <<EOF\ndon\nEOF$(git push)", "git push*"),
    ("cat <<EOF\n$(git push)", "git push*"),
    ("cat <<EOF\ndon't\nEOF\ngit push", "git push*"),
    # a double quote in place of the apostrophe
    ('cat <<EOF\ndon"t\nEOF$(git push)', "git push*"),
    ('cat <<EOF\nsay "hi\nEOF`git push`', "git push*"),
    # closed, the apostrophe before the substitution on the same line
    ("cat <<EOF\nit's $(git push)\nEOF", "git push*"),
    ("cat <<EOF\nit's `git push`\nEOF\necho done", "git push*"),
])
def test_unquoted_heredoc_quote_is_a_plain_character(command, expected):
    assert _deny_match(command, DENY) == expected


def test_quoted_heredoc_is_data_only_when_closed():
    closed = "cat <<'EOF'\ndon't\n$(git push)\nEOF"
    assert _deny_match(closed, DENY) is None
    assert _deny_match(closed + "\necho ok", DENY) is None
    # never closed: the reader cannot be sure, so it fails closed
    assert _deny_match("cat <<'EOF'\ndon't\n$(git push)", DENY) == "git push*"
    assert _deny_match("cat <<'EOF'\ndon't\nEOF$(git push)", DENY) == "git push*"


def test_dash_heredoc():
    assert _deny_match("cat <<-EOF\n\tdon't\n\tEOF$(git push)", DENY) == "git push*"
    assert _deny_match("cat <<-EOF\n\tit's $(git push)\n\tEOF", DENY) == "git push*"
    assert _deny_match("cat <<-'EOF'\n\tit's $(git push)\n\tEOF", DENY) is None


def test_escaped_substitution_and_quotes_inside_it():
    # \$( is literal in a heredoc body; a quoted ')' inside $(…) does not end it
    assert _deny_match("cat <<EOF\nit's \\$(git push)\nEOF", DENY) is None
    assert _deny_match("cat <<EOF\nx $(echo ')' ; git push)\nEOF", DENY) == "git push*"
