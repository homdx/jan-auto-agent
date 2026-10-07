"""Judge's acceptance suite for round 198 (the `deny_commands` shell reader), from the ticket alone.

Every case is a command line and whether `deny_commands = ["git push*"]` must refuse it, through the
one public entry `tools.contest.policy._deny_match`. The cases are the ticket's own (bugs 8-11 and
the rows it lists) plus the 164/187 behaviour it says must stay. A case whose expected answer the
ticket leaves open (a quoted heredoc's `$(…)`, an unclosed quote after `\\\\`) is not here.

Copy into an entry's checkout and run from there:
    python3 -m pytest contest-bench/198/acceptance_198.py -n 0 -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.contest.policy import _deny_match  # noqa: E402

DENY = ["git push*"]

DENIED = {
    # sanity, 164 / 187
    "plain": "git push",
    "and": "ls && git push",
    "semi": "ls; git push",
    "pipe": "echo x | git push",
    "subst": "echo $(git push)",
    "backtick": "echo `git push`",
    "nested3": "echo $(echo $(echo $(git push)))",
    "wrapper": "timeout 9 git push",
    # 8 — newline
    "nl": "echo hi\ngit push",
    "crlf": "echo hi\r\ngit push",
    "nl-blank": "echo hi\n\ngit push",
    "nl-tab": "echo hi\n\tgit push",
    # 9 — escaped quote
    "esc-dq": 'echo \\"; git push',
    "esc-sq-in-dq": "echo '\"'; git push",
    "esc-dq-in-dq": 'echo "a\\"b"; git push',
    # 10 — escaped ; stays an argument, but a real one after it still separates
    "esc-semi-then-real": "echo a \; b; git push",
    # heredoc: the line after the body, the rest of the delimiter's line
    "hd-after": "cat <<EOF\nhi\nEOF\ngit push",
    "hd-after-quoted": "cat <<'EOF'\nx\nEOF\ngit push",
    "hd-after-dash": "cat <<-EOF\n\thi\n\tEOF\ngit push",
    "hd-same-line": "cat <<EOF && git push\nhi\nEOF",
    "hd-two": "cat <<A <<B\nx\nA\ny\nB\ngit push",
    "hd-unterminated": "cat <<EOF\nx; git push",
    "hd-unquoted-subst": "cat <<EOF\n$(git push)\nEOF",
    "hd-unquoted-backtick": "cat <<EOF\n`git push`\nEOF",
    "hd-apostrophe": "cat <<EOF\ndon't\nEOF\necho $(git push)",
    "hd-lookalike-here-string": "cat <<< 'x'; git push",
    "hd-lookalike-arith": "echo $((1 << 2)); git push",
    # 11 — a parenthesis in quotes inside $( … )
    "subst-sq-rparen": "echo \"$(printf ')' ; git push)\"",
    "subst-sq-lparen": "echo \"$(printf '(' ; git push)\"",
    "subst-esc-rparen": "echo $(echo \\) ; git push)",
    "subst-dq-rparen": "echo $(echo \")\" ; git push)",
}

ALLOWED = {
    "plain-other": "git status",
    "echo": "echo hi",
    "sq-literal": "echo '$(git push)'",
    "esc-semi": "echo \; git push",
    "find-exec": "find . -exec ls {} \;",
    "hd-body-semi": "cat > f <<'EOF'\nx; git push\nEOF",
    "hd-body-semi-unquoted": "cat > f <<EOF\nx; git push\nEOF",
    "hd-body-dash": "cat <<-EOF\n\tx; git push\n\tEOF",
    "hd-plain": "cat <<EOF\nhi\nEOF",
    "hd-two-bodies": "cat <<A <<B\nx; git push\nA\ny; git push\nB",
    "hd-body-newline-push": "cat <<'EOF'\nfoo\ngit push\nEOF",
}


@pytest.mark.parametrize("name", sorted(DENIED))
def test_denied(name):
    assert _deny_match(DENIED[name], DENY) is not None, repr(DENIED[name])


@pytest.mark.parametrize("name", sorted(ALLOWED))
def test_allowed(name):
    assert _deny_match(ALLOWED[name], DENY) is None, repr(ALLOWED[name])


def test_empty_and_other_patterns():
    assert _deny_match("", DENY) is None
    assert _deny_match("echo hi\ngit push", ["rm -rf*"]) is None
    assert _deny_match("echo hi\nrm -rf /", ["rm -rf*"]) is not None
