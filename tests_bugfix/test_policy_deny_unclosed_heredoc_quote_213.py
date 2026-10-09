"""213 follow-up: an unclosed heredoc is read line by line — a quote in one line never hides the next."""
import pytest

from tools.contest.policy import _deny_match

DENY = ["git push*"]


@pytest.mark.parametrize("command", [
    "cat <<EOF\ndon't\ngit push",
    "cat <<EOF\nit's\ngit push origin",
    'cat <<EOF\nsay "hi\ngit push',
    "cat <<'EOF'\ndon't\ngit push",
])
def test_unclosed_heredoc_fails_closed_past_a_quote_character(command):
    assert _deny_match(command, DENY) == "git push*"


def test_a_closed_heredoc_stays_data():
    assert _deny_match("cat <<EOF\ndon't\ngit push\nEOF\necho ok", DENY) is None
    assert _deny_match("cat <<'EOF'\nit's\ngit push\nEOF", DENY) is None
