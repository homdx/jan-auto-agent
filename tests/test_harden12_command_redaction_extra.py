"""HARDEN-12 D (judge's additions): `--user`, and a variable named for a count or a path is no credential."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.auto.executor import _redact_command  # noqa: E402


def test_the_long_user_option_is_redacted_like_the_short_one():
    for cmd in ("curl --user admin:secretpass1234 http://x", "curl --user=admin:secretpass1234 http://x", "curl -u admin:secretpass1234 http://x"):
        out = _redact_command(cmd)
        assert "secretpass1234" not in out and "admin" in out


def test_a_variable_named_for_a_count_or_a_path_is_kept():
    for cmd in ("TOKEN_COUNT=5 python x.py", "KEY_DIR=/tmp/keys ./run", "API_KEY_NAME=prod make", "SECRET_FILE=/etc/s ./go"):
        assert _redact_command(cmd) == cmd
    assert "abc123456789" not in _redact_command("API_KEY=abc123456789 python x.py")
