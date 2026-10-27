"""v13 (CMD-1, residual forms): the long `--password='…'` option and the `-u 'user:pass'` argument also cut a
quoted password at its first space (the `-p` and `-a` forms are in test_v12_*). The quoted word is now redacted
whole; a quoted uid:gid (`-u '1000:1000'`) and an unquoted `-u 1000:1000` are left alone."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.executor import _redact_command  # noqa: E402


@pytest.mark.parametrize("command,expected", [
    ("mysql --password='my secret' mydb", "mysql --password=[REDACTED] mydb"),
    ('mysql --password="my secret" mydb', "mysql --password=[REDACTED] mydb"),
    ("curl -u 'admin:my secret' https://x.example", "curl -u 'admin:[REDACTED]' https://x.example"),
    ('curl -u "admin:my secret" https://x.example', 'curl -u "admin:[REDACTED]" https://x.example'),
    ("curl -u admin:Passw0rd https://x.example", "curl -u admin:[REDACTED] https://x.example"),
])
def test_a_quoted_password_is_redacted_whole(command, expected):
    assert _redact_command(command) == expected


@pytest.mark.parametrize("command", [
    "docker run -u 1000:1000 img",
    "docker run -u '1000:1000' img",
])
def test_a_uid_and_gid_are_not_a_password(command):
    assert _redact_command(command) == command
