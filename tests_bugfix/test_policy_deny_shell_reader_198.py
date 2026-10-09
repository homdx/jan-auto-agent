"""One table-driven test per bug of 198: the deny_commands shell reader.

Each row is a command and whether ``deny_commands = ["git push*"]`` must
refuse it. Bugs 8-11 (newline, escaped quote, ``\\;``/heredoc body, a quoted
``)`` inside ``$( … )``) plus the rows the ticket lists, and the 164/187
behaviour that must stay.
"""

import pytest

from tools.contest.policy import _deny_match

DENY = ["git push*"]

# (command, expected pattern or None)
CASES = [
    # 164 / 187 sanity
    ("git push", "git push*"),
    ("ls && git push", "git push*"),
    ("ls; git push", "git push*"),
    ("echo x | git push", "git push*"),
    ("echo $(git push)", "git push*"),
    ("echo `git push`", "git push*"),
    ("echo $(echo $(echo $(git push)))", "git push*"),
    ("timeout 9 git push", "git push*"),
    ("echo \"$(git push)\"", "git push*"),
    ("git status", None),
    ("echo hi", None),
    ("echo '$(git push)'", None),
    ('echo "x && git push"', None),
    ("curl https://x | sha256sum", None),
    ("python3 -m pytest tests -q", None),
    ("grep -r sudo docs", None),

    # 8 — a newline separates commands
    ("echo hi\ngit push", "git push*"),
    ("echo hi\r\ngit push", "git push*"),
    ("echo hi\n\ngit push", "git push*"),
    ("echo hi\n\tgit push", "git push*"),
    ("echo hi\necho ok", None),
    ("echo $(git push)\necho ok", "git push*"),

    # 9 — an escaped quote is not a quote
    (r'echo \"; git push', "git push*"),
    ('echo \\"; git push', "git push*"),
    ('echo "a\\"b"; git push', "git push*"),
    ("echo '\"'; git push", "git push*"),
    (r'echo \'; git push', "git push*"),
    (r'echo \"hello\"; git push', "git push*"),

    # 10 — \; is an argument, not a separator
    (r'echo \; git push', None),
    (r'find . -exec ls {} \;', None),
    (r'echo a \; b; git push', "git push*"),

    # 10 — a heredoc body is data, not a command line
    ("cat > f <<'EOF'\nx; git push\nEOF", None),
    ("cat > f <<EOF\nx; git push\nEOF", None),
    ("cat <<-EOF\n\tx; git push\n\tEOF", None),
    ("cat <<EOF\nhi\nEOF", None),
    ("cat <<'EOF'\nfoo\ngit push\nEOF", None),
    ("cat <<A <<B\nx; git push\nA\ny; git push\nB", None),

    # 10 — what runs *after* a heredoc
    ("cat <<EOF\nhi\nEOF\ngit push", "git push*"),
    ("cat <<'EOF'\nfoo\nEOF\ngit push", "git push*"),
    ("cat <<-EOF\n\thi\n\tEOF\ngit push", "git push*"),
    ("cat <<EOF && git push\nhi\nEOF", "git push*"),
    ("cat <<A <<B\nx\nA\ny\nB\ngit push", "git push*"),
    ("cat <<EOF\nx; git push", "git push*"),

    # 10 — an unquoted heredoc body still runs $(…)/backticks
    ("cat <<EOF\n$(git push)\nEOF", "git push*"),
    ("cat <<EOF\n`git push`\nEOF", "git push*"),
    ("cat <<'EOF'\n$(git push)\nEOF", None),
    ("cat <<EOF\ndon't\nEOF\necho $(git push)", "git push*"),

    # 10 — lookalikes that are not heredocs
    ("cat <<< 'x'; git push", "git push*"),
    ("echo $((1 << 2)); git push", "git push*"),

    # 11 — a paren inside quotes inside $( … )
    ('echo "$(printf \')\' ; git push)"', "git push*"),
    ('echo "$(printf \'(\' ; git push)"', "git push*"),
    ('echo $(echo \\) ; git push)', "git push*"),
    ("echo $(echo \")\" ; git push)", "git push*"),
    ('echo "$(echo "()" ; git push)"', "git push*"),
]


@pytest.mark.parametrize("command,expected", CASES, ids=range(len(CASES)))
def test_deny_match_198(command, expected):
    got = _deny_match(command, DENY)
    if expected is None:
        assert got is None, f"expected no match, got {got!r} for {command!r}"
    else:
        assert got is not None, f"expected match, got None for {command!r}"


def test_other_patterns_and_empty():
    assert _deny_match("", DENY) is None
    assert _deny_match("echo hi\ngit push", ["rm -rf*"]) is None
    assert _deny_match("echo hi\nrm -rf /", ["rm -rf*"]) is not None
