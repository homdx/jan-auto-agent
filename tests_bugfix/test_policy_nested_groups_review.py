"""tests_bugfix/test_policy_nested_groups_review.py — `deny_commands` and nested shell groups.

`_deny_candidates` documents that a leading `(`/`{`/`!` and a trailing `)`/`}` are
dropped so the command inside a group is matched as the command it is. It only
stripped them from the first and last *token*, so a group that was nested or
negated — `( ( git push ) )`, `! ( git push )`, `{ { git push; }; }` — kept a bare
`(` in front and matched no deny pattern.
"""

from __future__ import annotations

import pytest

from tools.contest import policy

DENY = ["git push"]


@pytest.mark.parametrize("command", [
    "git push",
    "( git push )",
    "(git push)",
    "{ git push; }",
    "! git push",
    "( ( git push ) )",
    "( (git push) )",
    "((git push))",
    "! ( git push )",
    "{ { git push; }; }",
    "{ ( git push ); }",
    "( { git push; } )",
    "( ( timeout 9 git push ) )",
    "cd x && ( ( git push ) )",
])
def test_a_denied_command_is_caught_however_deeply_it_is_grouped(command):
    assert policy._deny_match(command, DENY) == "git push"


@pytest.mark.parametrize("command", [
    "( ( git status ) )",
    "! ( git log )",
    "{ { git pull; }; }",
    "( ( echo push ) )",
])
def test_a_grouped_command_that_is_not_denied_stays_allowed(command):
    assert policy._deny_match(command, DENY) is None
