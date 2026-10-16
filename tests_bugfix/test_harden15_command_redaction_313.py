"""tests_bugfix/test_harden15_command_redaction_313.py — HARDEN-15 C: three corrections
to `_redact_command`, each of which the HARDEN-12 D table already covered in the other
direction.

Reproduced on 277d632: `redis-cli -a hunter2secret ping` and `aws configure set
aws_secret_access_key AbCdEf123456` were logged as written, `redis-cli --pass pw` and
`redis-cli -u redis://:pw@host` were logged as written, and `mysql -p mydb` — where
`mydb` is the database, since MySQL's `-p` is the prompt flag and the password is
attached — went out as `mysql -p [REDACTED]`. Logs only, but they are archived.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.auto.executor import _redact_command  # noqa: E402


class TestRedisCliPasswords:
    """`redis-cli -a <pw>` and `--pass <pw>`; `redis-cli -u` with a URL that has no user."""

    @pytest.mark.parametrize("command, secret", [
        ("redis-cli -a hunter2secret ping", "hunter2secret"),
        ("redis-cli -a hunter2secret -h localhost ping", "hunter2secret"),
        ("redis-cli --pass hunter2secret ping", "hunter2secret"),
        ("redis-cli --pass=hunter2secret ping", "hunter2secret"),
        ("redis-cli -u redis://:hunter2secret@host ping", "hunter2secret"),
        ("redis-cli -u rediss://user:hunter2secret@host ping", "hunter2secret"),
    ])
    def test_the_password_is_out_of_the_log(self, command, secret):
        out = _redact_command(command)
        assert secret not in out, command
        assert "[REDACTED]" in out, command

    def test_the_flag_name_and_the_rest_of_the_command_stay(self):
        assert _redact_command("redis-cli -a hunter2secret ping") == \
            "redis-cli -a [REDACTED] ping"
        assert _redact_command("redis-cli --pass hunter2secret ping") == \
            "redis-cli --pass [REDACTED] ping"

    def test_a_url_with_no_user_keeps_its_shape(self):
        # The password goes, but `redis://` does not become `redis:` and the host stays.
        out = _redact_command("redis-cli -u redis://:hunter2secret@host ping")
        assert out == "redis-cli -u redis://:[REDACTED]@host ping"
        assert "redis://" in out

    def test_a_url_with_a_user_keeps_the_user(self):
        out = _redact_command("redis-cli -u rediss://user:hunter2secret@host ping")
        assert out == "redis-cli -u rediss://user:[REDACTED]@host ping"
        assert "user" in out

    @pytest.mark.parametrize("command", [
        "redis-cli -h localhost -p 6380 ping",
        "redis-cli -a 1",
        "redis-cli -u redis://host/ping",
        "redis-cli -u someflag ping",
    ])
    def test_a_command_without_a_password_is_byte_for_byte(self, command):
        assert _redact_command(command) == command

    @pytest.mark.parametrize("command", [
        "ls -a",
        "ls -al /tmp",
        "grep -a pattern file.txt",
        "sort -a file.txt",
        "tar -a archive.tar",
    ])
    def test_an_a_elsewhere_is_not_a_password(self, command):
        assert _redact_command(command) == command


class TestAwsConfigureSet:
    """`aws configure set <name> <value>` writes the named credential to the credential
    file; the name says what it is, so the value is the secret."""

    @pytest.mark.parametrize("command, secret", [
        ("aws configure set aws_secret_access_key AbCdEf123456", "AbCdEf123456"),
        ("aws configure set aws_secret_access_key AbCdEf123456 --profile prod",
         "AbCdEf123456"),
        ("aws configure set aws_access_key_id AKIAIOSFODNN7EXAMPLE", "AKIAIOSFODNN7EXAMPLE"),
        ("aws configure set aws_session_token tokenabc1234567890", "tokenabc1234567890"),
    ])
    def test_the_value_is_out_of_the_log(self, command, secret):
        out = _redact_command(command)
        assert secret not in out, command
        assert "[REDACTED]" in out, command

    def test_the_name_stays_visible(self):
        assert _redact_command("aws configure set aws_secret_access_key AbCdEf123456") == \
            "aws configure set aws_secret_access_key [REDACTED]"

    def test_the_rest_of_the_command_stays(self):
        out = _redact_command("aws configure set aws_secret_access_key AbCdEf123456 --profile prod")
        assert out == "aws configure set aws_secret_access_key [REDACTED] --profile prod"

    @pytest.mark.parametrize("command", [
        "aws configure set default region us-east-1",
        "aws configure set default output json",
        "aws configure get aws_region",
        "aws s3 cp local.txt s3://bucket/key --profile prod",
        "echo set secret abc",
        "git config --global user.name set key value",
    ])
    def test_a_name_that_does_not_name_a_credential_stays(self, command):
        assert _redact_command(command) == command


class TestMySQLPasswordForms:
    """A `-p` followed by a space is MySQL's prompt flag, so the next word is the database;
    the password is attached (`-psecret`)."""

    @pytest.mark.parametrize("command", [
        "mysql -p mydb",
        "mysqldump -p mydb",
        "mariadb -p mydb",
        "mysql -p my_database",
        "mysql -p production --databases production",
        "mysql -p db",
        "mysql -p5432",
    ])
    def test_the_database_is_kept(self, command):
        assert _redact_command(command) == command

    @pytest.mark.parametrize("command", [
        "mysql -psecretpass1234",
        "mysql -psecretpass1234 db",
        "mysqldump -psecretpass1234 mydb",
        "mysql -p secretpass1234 db",
        "mariadb -psecretpass1234 db",
    ])
    def test_the_attached_form_is_still_a_password(self, command):
        out = _redact_command(command)
        assert "secretpass1234" not in out, command
        assert "[REDACTED]" in out, command

    def test_a_database_named_with_a_digit_is_still_kept(self):
        assert _redact_command("mysql -p db_orders mydb") == "mysql -p db_orders mydb"


class TestTheLongPasswordOption:
    """`--pass` and `--password` are long options, so they are redacted for any program."""

    @pytest.mark.parametrize("command", [
        "docker login --password hunter2secret",
        "docker login --password=hunter2secret",
        "docker login -u me --password hunter2secret",
        "somecli --pass hunter2secret",
    ])
    def test_the_value_is_redacted(self, command):
        assert "hunter2secret" not in _redact_command(command)
        assert "[REDACTED]" in _redact_command(command)

    @pytest.mark.parametrize("command", [
        "docker login --password-stdin",
        "docker login --password-stdin < cred.txt",
    ])
    def test_password_stdin_takes_no_value(self, command):
        assert _redact_command(command) == command


class TestUnchanged:
    """The HARDEN-12 D table, unchanged."""

    @pytest.mark.parametrize("command, expected", [
        ("curl -u user:secretpass1234 http://x", "curl -u user:[REDACTED] http://x"),
        ("curl --user admin:secretpass1234 http://x", "curl --user admin:[REDACTED] http://x"),
        ("docker login -p secretpass1234", "docker login -p [REDACTED]"),
        ("mysql -psecretpass1234", "mysql -p[REDACTED]"),
        ("mysql -p secretpass1234 db", "mysql -p [REDACTED] db"),
        ("git clone https://user:tokenabc12345@host/r.git",
         "git clone https://user:[REDACTED]@host/r.git"),
        ("pytest --api-key=abc12345", "pytest --api-key=[REDACTED]"),
    ])
    def test_the_forms_that_already_worked(self, command, expected):
        assert _redact_command(command) == expected

    @pytest.mark.parametrize("command", [
        "ls -p",
        "grep -p pat file.txt",
        "pytest -p no:cacheprovider",
        "docker run -p 8080:80 nginx",
        "curl -u someflag http://x",
        "git clone https://host/r.git",
        "FOO=bar pytest -q",
        "grep foo=bar file.txt",
        "curl -H 'Content-Type: application/json' http://x",
        "ls -la /tmp",
        "python -c 'x = 1'",
        "git@github.com:org/repo.git",
        "env PATH=/usr/bin pytest -q",
        "TOKEN_COUNT=5 python x.py",
        "KEY_DIR=/tmp/keys ./run",
    ])
    def test_a_command_without_a_secret_is_byte_for_byte(self, command):
        assert _redact_command(command) == command
