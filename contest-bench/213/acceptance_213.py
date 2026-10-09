"""Black-box bench for ticket 213 (an apostrophe/double quote in an unquoted heredoc body hides a `$(…)`), from the ticket alone.

Every case is a command line and whether `deny_commands = ["git push*"]` must refuse it, through the one
public entry `tools.contest.policy._deny_match`. The ticket's four lines, the double-quote twin, the
`<<-EOF` form, backticks, and the controls (a quoted heredoc's body is data; a body without a substitution
allows).

Run from a checkout root:  python3 -m pytest contest-bench/213/acceptance_213.py -n 0 -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.contest.policy import _deny_match  # noqa: E402

DENY = ["git push*"]

DENIED = {
    # the ticket's four lines
    "apos-open-term-subst": "cat <<EOF\ndon't\nEOF$(git push)",
    "no-apos-open-term-subst": "cat <<EOF\ndon\nEOF$(git push)",
    "unterminated-subst": "cat <<EOF\n$(git push)",
    "apos-closed-then-push": "cat <<EOF\ndon't\nEOF\ngit push",
    # a double quote in place of the apostrophe
    "dq-open-term-subst": 'cat <<EOF\ndon"t\nEOF$(git push)',
    "dq-unterminated-subst": 'cat <<EOF\nsay "hi $(git push)',
    "dq-closed-then-push": 'cat <<EOF\ndon"t\nEOF\ngit push',
    # <<-EOF
    "dash-apos-open-term-subst": "cat <<-EOF\n\tdon't\n\tEOF$(git push)",
    "dash-apos-closed-then-push": "cat <<-EOF\n\tdon't\nEOF\ngit push",
    "dash-apos-subst-body": "cat <<-EOF\n\tdon't $(git push)\n\tEOF",
    # backticks
    "apos-backtick-term": "cat <<EOF\ndon't\nEOF`git push`",
    "apos-backtick-unterminated": "cat <<EOF\ndon't `git push`",
    # the apostrophe on the line before and on the same line as the substitution
    "apos-then-subst-same-line": "cat <<EOF\ndon't $(git push)\nEOF",
    "apos-then-subst-next-line": "cat <<EOF\ndon't\n$(git push)\nEOF",
    "apos-twice-then-subst": "cat <<EOF\ndon't won't\nEOF$(git push)",
    # the substitution is reached after text that is not a terminator
    "terminator-with-trailing-space": "cat <<EOF\ndon't\nEOF $(git push)",
    "unterminated-apos-semicolon": "cat <<EOF\ndon't\nx; $(git push)",
}

ALLOWED = {
    # quoted heredoc: the body is data, closed
    "quoted-closed-subst": "cat <<'EOF'\ndon't $(git push)\nEOF",
    "quoted-closed-subst-dq": 'cat <<"EOF"\ndon\'t $(git push)\nEOF',
    "quoted-closed-plain": "cat <<'EOF'\ndon't; git push\nEOF",
    "dash-quoted-closed": "cat <<-'EOF'\n\tdon't $(git push)\n\tEOF",
    # an unquoted body with an apostrophe and no substitution, closed
    "apos-closed-nothing": "cat <<EOF\ndon't\nEOF",
    "apos-closed-text-push": "cat <<EOF\ndon't x; git push\nEOF",
    "dq-closed-text-push": 'cat <<EOF\ndon"t git push\nEOF',
    "apos-closed-harmless-subst": "cat <<EOF\ndon't $(echo hi)\nEOF",
    "dash-apos-closed-nothing": "cat <<-EOF\n\tdon't\n\tEOF",
}


@pytest.mark.parametrize("case", sorted(DENIED))
def test_denied(case):
    assert _deny_match(DENIED[case], DENY) is not None, repr(DENIED[case])


@pytest.mark.parametrize("case", sorted(ALLOWED))
def test_allowed(case):
    assert _deny_match(ALLOWED[case], DENY) is None, repr(ALLOWED[case])
