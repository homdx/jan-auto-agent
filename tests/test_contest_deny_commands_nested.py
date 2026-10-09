"""deny_commands also matches a command run inside $(...), backticks, <(...), bash -c and eval."""
import pytest

from tools.contest.policy import _deny_match

DENY = ["git push*"]


@pytest.mark.parametrize("command", [
    "echo $(git push)",
    "echo `git push origin x`",
    "diff <(git push) a",
    'echo "$(git push)"',
    "echo $(echo $(git push))",
    "bash -c 'git push'",
    'sh -lc "cd x && git push"',
    "timeout 9 bash -c 'git push'",
    "eval 'git push'",
])
def test_nested_command_is_denied(command):
    assert _deny_match(command, DENY) == "git push*"


@pytest.mark.parametrize("command", [
    "echo '$(git push)'",          # single quotes: literal text, nothing runs
    "echo hi",
    "echo $(git status)",
    "bash -c 'git status'",
    "echo $(unclosed",             # malformed: must not raise
    "bash -c",
])
def test_harmless_or_malformed_command_is_not_denied(command):
    assert _deny_match(command, DENY) is None
