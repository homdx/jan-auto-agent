"""v12: a shell-quoted password that contains a space was logged after its first word. `-p` and `-a`
took the value as a run of non-space characters, so `'my secret'` was judged as `'my` (too short to be a
password) and `secret'` went out with the command. The value is now the whole quoted word."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.executor import _redact_command  # noqa: E402


@pytest.mark.parametrize("command,expected", [
    ("mysql -p'my secret' mydb", "mysql -p[REDACTED] mydb"),
    ('mysql -p"my secret" mydb', "mysql -p[REDACTED] mydb"),
    ("redis-cli -a 'my secret' get key", "redis-cli -a [REDACTED] get key"),
    ('redis-cli -a "my secret" get key', "redis-cli -a [REDACTED] get key"),
])
def test_a_quoted_password_with_a_space_is_redacted_whole(command, expected):
    assert _redact_command(command) == expected


@pytest.mark.parametrize("command,expected", [
    ("mysql -p mydb", "mysql -p mydb"),
    ("mysql -psupersecret mydb", "mysql -p[REDACTED] mydb"),
    ("mysql -u root -p SuperSecret123 -h db.internal", "mysql -u root -p [REDACTED] -h db.internal"),
    ("redis-cli -a SuperSecret123 get key", "redis-cli -a [REDACTED] get key"),
])
def test_the_unquoted_forms_are_unchanged(command, expected):
    assert _redact_command(command) == expected
