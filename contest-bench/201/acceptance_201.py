"""Black-box bench for ticket 201 (`deny_commands` shell reader, second pass), from the ticket alone.

Every case is a command line and whether `deny_commands = ["git push*"]` must refuse it, through the one
public entry `tools.contest.policy._deny_match`. Bugs 12 (`$'…'`), 13 (a quoted `)` in the `$( … )` of an
unquoted heredoc body), 14 (`#` comments), 15 (a heredoc delimiter quoted by `\\` or in parts), plus the
controls the ticket lists as "right today". 198's own bench is run next to it (`run_201.sh`).

Run from a checkout root:  python3 -m pytest contest-bench/201/acceptance_201.py -n 0 -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.contest.policy import _deny_match  # noqa: E402

DENY = ["git push*"]

DENIED = {
    # 12 — $'…'
    "ansi-esc-quote": "echo $'a\\'b'; git push",
    "ansi-lone-esc-quote": "echo $'\\''; git push",
    "ansi-esc-backslash-closes": "echo $'a\\\\'; git push",
    "ansi-bash-c": "bash -c $'git push'",
    "ansi-eval": "eval $'git push'",
    "ansi-bash-c-hex-space": "bash -c $'git\\x20push'",
    "ansi-bash-c-octal-space": "bash -c $'git\\040push'",
    "ansi-bash-c-newline": "bash -c $'echo a\\ngit push'",
    "ansi-bash-c-tab": "bash -c $'git\\tpush'",
    "ansi-control-plain": "echo $'x'; git push",
    "ansi-control-semi": "echo $'a;b' ; git push",
    "ansi-control-dollar-dq": 'echo $"a"; git push',
    "ansi-two-strings": "echo $'a\\'b' $'c\\'d'; git push",
    # 13 — a quoted ) in the $( … ) of an unquoted heredoc body
    "hd-subst-sq-paren": "cat <<EOF\n$(echo ')'; git push)\nEOF",
    "hd-subst-dq-paren": 'cat <<EOF\n$(echo ")"; git push)\nEOF',
    "hd-subst-esc-paren": "cat <<EOF\n$(echo \\); git push)\nEOF",
    "hd-backtick-sq-paren": "cat <<EOF\n`echo ')'; git push`\nEOF",
    "hd-subst-plain": "cat <<EOF\n$(git push)\nEOF",
    "hd-subst-after-apostrophe": "cat <<EOF\ndon't $(git push)\nEOF",
    "hd-subst-nested-quote": "cat <<EOF\n$(echo $(echo ')'); git push)\nEOF",
    "top-subst-sq-paren": "echo \"$(echo ')'; git push)\"",
    # 14 — comments
    "cmt-heredoc-in-comment": "echo a # <<EOF\ngit push",
    "cmt-heredoc-no-space": "echo a #<<EOF\ngit push",
    "cmt-after-semi": "echo a;# x\ngit push",
    "cmt-hash-in-word": "echo a#b; git push",
    "cmt-dollar-hash": "echo $#; git push",
    "cmt-param-length": "echo ${#x}; git push",
    "cmt-hash-in-dq": 'echo "a # b"; git push',
    "cmt-hash-in-sq": "echo 'a # b'; git push",
    "cmt-comment-then-real-line": "echo a # note\ngit push",
    # 15 — quoted delimiter, then a real command after the terminator
    "dl-bs-after": "cat <<\\EOF\nx\nEOF\ngit push",
    "dl-parts-after": 'cat <<E"O"F\nx\nEOF\ngit push',
    "dl-sq-parts-after": "cat <<'E'OF\nx\nEOF\ngit push",
    "dl-dq-after": 'cat <<"EOF"\nx\nEOF\ngit push',
    "dl-unquoted-after": "cat <<EOF\nx\nEOF\ngit push",
}

ALLOWED = {
    # 12
    "ansi-all-inside": "echo $'a; git push'",
    "ansi-esc-quote-inside": "echo $'a\\'; git push'",
    "ansi-bash-c-harmless": "bash -c $'echo hi'",
    "ansi-eval-harmless": "eval $'echo hi'",
    # 13
    "hd-quoted-data": "cat <<'EOF'\n$(echo ')'; git push)\nEOF",
    "hd-quoted-data-backtick": "cat <<'EOF'\n`echo ')'; git push`\nEOF",
    "hd-harmless-quoted-paren": "cat <<EOF\n$(echo ')')\nEOF",
    "hd-harmless-text": "cat <<EOF\nit's x; git push\nEOF",
    # 14
    "cmt-semi-in-comment": "echo a # ; git push",
    "cmt-dq-hash-then-heredoc": 'echo "#" <<EOF\nx; git push\nEOF',
    "cmt-hash-in-word-then-heredoc": "echo a#b <<EOF\nx; git push\nEOF",
    "cmt-dollar-hash-then-heredoc": "echo $# <<EOF\nx; git push\nEOF",
    "cmt-whole-comment-line": "# git push",
    "cmt-comment-mentions-push": "echo a # git push later",
    # 15 — the body is data when any part of the delimiter word was quoted
    "dl-bs-data": "cat <<\\EOF\nx; git push\nEOF",
    "dl-parts-data": 'cat <<E"O"F\nx; git push\nEOF',
    "dl-sq-parts-data": "cat <<'E'OF\nx; git push\nEOF",
    "dl-dq-data": 'cat <<"EOF"\nx; git push\nEOF',
    "dl-bs-subst-data": "cat <<\\EOF\n$(git push)\nEOF",
    "dl-parts-subst-data": 'cat <<E"O"F\n$(git push)\nEOF',
    "dl-unquoted-text": "cat <<EOF\nx; git push\nEOF",
    "dl-dash-bs-data": "cat <<-\\EOF\n\tx; git push\n\tEOF",
}


@pytest.mark.parametrize("case", sorted(DENIED))
def test_denied(case):
    assert _deny_match(DENIED[case], DENY) is not None, repr(DENIED[case])


@pytest.mark.parametrize("case", sorted(ALLOWED))
def test_allowed(case):
    assert _deny_match(ALLOWED[case], DENY) is None, repr(ALLOWED[case])
