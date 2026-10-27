"""tests_bugfix/test_harden19_credential_names_and_quoted_values.py — HARDEN-19: a credential
variable whose name carries the credential word as one word or in camelCase was not a
credential, in the mask and in the command log alike, and a shell-quoted password with a
space was logged after its first word.

An outside review of 5c403f1 found that CC review fix 40 went too far. Fix 40 made the
credential word a *whole* part of the name, so that `TOKENIZERS_PARALLELISM` and
`KEYBOARD_LAYOUT` are not credentials — and in doing so it threw away `STRIPEKEY`,
`GITHUBTOKEN`, `AUTHTOKEN`, `SECRETKEY`, `clientSecret` and `dbPassword`: 24 of 28
credential names reached the model unmasked on the environ subscript, and 18 of 29
credential-named variables leaked into the command log, `PGPASSWORD=… psql` among them.

* A: the environ mask now splits a name into its words — on `_`, `-`, `.`, and at the
  camelCase boundary — and a credential word counts either as a whole word or as the name's
  tail written together. One helper serves the mask and the command log.
* B: `_SECRET_ENV_ASSIGN_RE` and the header rule ask that same helper, and a name that is a
  delimited path and sits mid-command is looked at too, npm's `:_authToken=` among them.
* C: one value pattern — a single-quoted word, a double-quoted word, or a run of
  non-space characters — behind `-p`, `-a`, the long option and `-u user:pass`. The whole
  token is redacted, quotes included, and an unterminated quote runs to the end of the line.

The CC fix 40 controls stay green, and so do the HARDEN-12 B and D tables.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.llm_stream import (  # noqa: E402
    _credential_name_words,
    _name_is_a_credential,
    _name_is_not_a_credential,
    mask_api_key,
    restore_masked_secrets,
)
from tools.auto.executor import _redact_command  # noqa: E423


_MASKED = "here_your_key"
_HEX = "0123456789abcdef0123456789abcdef"

#: The names of the ticket: each holds a credential whatever its letters look like.
_CREDENTIAL_NAMES = (
    "STRIPEKEY", "GITHUBTOKEN", "SYSTEM_ACCESSTOKEN", "AUTHTOKEN", "SECRETKEY",
    "OPENAIKEY", "APIKEY", "OpenAIKey", "GitHubToken", "clientSecret", "dbPassword",
    "PGPASSWORD", "DBPASSWORD", "MY_PASSWD", "MY_PWD", "AUTH_TOKEN", "X-Auth-Token",
    "NGROK_AUTHTOKEN", "DBPASSWD", "ACCESSKEY", "client_secret", "access_key",
)

#: The names that are not credentials — fix 40's cases, the stoplist and the count-and-path
#: suffixes. A credential word inside a name does not make it a credential.
_NOT_CREDENTIALS = (
    "TOKENIZERS_PARALLELISM", "KEYBOARD_LAYOUT", "MONKEY_PATCH", "MAX_TOKENS",
    "PRIMARY_KEY", "KEYMAP", "MONKEY", "TURKEY", "HOTKEY", "DONKEY", "KEYBOARD",
    "KEY_COUNT", "API_KEY_NAME", "SECRET_FILE", "PASSWORD_LENGTH", "TOKEN_COUNT",
    "KEY_DIR", "TOKENIZER", "KEYBOARD", "DATABASE_URL", "PGHOST", "USER", "HOME",
)


class TestANameIsReadByItsWords:
    """Part A: the one name rule, and the forms that carry it."""

    @pytest.mark.parametrize("name", _CREDENTIAL_NAMES)
    def test_each_credential_name_counts(self, name):
        assert _name_is_a_credential(name)
        assert not _name_is_not_a_credential(name)

    @pytest.mark.parametrize("name", _NOT_CREDENTIALS)
    def test_a_false_friend_is_not_a_credential(self, name):
        assert not _name_is_a_credential(name)
        assert _name_is_not_a_credential(name)

    def test_the_words_come_out_of_underscores_and_camelcase(self):
        assert _credential_name_words("SYSTEM_ACCESSTOKEN") == ["system", "accesstoken"]
        assert _credential_name_words("clientSecret") == ["client", "secret"]
        assert _credential_name_words("STRIPEKEY") == ["stripekey"]
        assert _credential_name_words("OpenAIKey") == ["open", "ai", "key"]
        assert _credential_name_words("NGROK-AUTH_TOKEN") == ["ngrok", "auth", "token"]

    def test_a_malformed_name_is_not_a_credential(self):
        for bad in ("", None, 5, ["TOKEN"], "", " ", "-", "._-"):
            assert _name_is_a_credential(bad) is False

    @pytest.mark.parametrize("name", _CREDENTIAL_NAMES)
    def test_a_credential_name_masks_in_the_subscript(self, name):
        line = f'os.environ["{name}"] = "{_HEX}"'
        out = mask_api_key(line)
        assert out == f'os.environ["{name}"] = "{_MASKED}"'
        assert _HEX not in out

    @pytest.mark.parametrize("line", _NOT_CREDENTIALS)
    def test_a_false_friend_stays_in_the_subscript(self, line):
        assert mask_api_key(f'os.environ["{line}"] = "{_HEX}"') == f'os.environ["{line}"] = "{_HEX}"'

    @pytest.mark.parametrize("line, expected", [
        (f'os.environ["STRIPEKEY"] = "{_HEX}"', f'os.environ["STRIPEKEY"] = "{_MASKED}"'),
        (f"os.environ['STRIPEKEY'] = '{_HEX}'", f"os.environ['STRIPEKEY'] = '{_MASKED}'"),
        (f'os.environ.setdefault("STRIPEKEY", "{_HEX}")', f'os.environ.setdefault("STRIPEKEY", "{_MASKED}")'),
        (f'process.env["STRIPEKEY"] = "{_HEX}"', f'process.env["STRIPEKEY"] = "{_MASKED}"'),
        (f'ENV["STRIPEKEY"] = "{_HEX}"', f'ENV["STRIPEKEY"] = "{_MASKED}"'),
        (f'$_ENV["STRIPEKEY"] = "{_HEX}"', f'$_ENV["STRIPEKEY"] = "{_MASKED}"'),
        (f'System.setProperty("api.key", "{_HEX}")', f'System.setProperty("api.key", "{_MASKED}")'),
        (f'os.putenv("PGPASSWORD", "{_HEX}")', f'os.putenv("PGPASSWORD", "{_MASKED}")'),
        (f'os.getenv("STRIPEKEY", "{_HEX}")', f'os.getenv("STRIPEKEY", "{_MASKED}")'),
    ])
    def test_the_access_forms_of_one_name_mask_alike(self, line, expected):
        assert mask_api_key(line) == expected

    @pytest.mark.parametrize("name", _NOT_CREDENTIALS)
    def test_the_same_false_friend_stays_in_every_form(self, name):
        for template in (f'os.environ["{name}"] = "{_HEX}"',
                         f'os.environ.setdefault("{name}", "{_HEX}")',
                         f'process.env["{name}"] = "{_HEX}"'):
            assert mask_api_key(template) == template

    @pytest.mark.parametrize("line", [
        'System.getenv("api.key")',
        'os.getenv("API_KEY")',
        'dotenv.setdefault("TOKEN", "AbCdEf0123456789")',
        'config.setdefault("API_KEY", "AbCdEf0123456789")',
        'os.environ.get("API_KEY")',
        'os.environ.setdefault("API_KEY", os.getenv("OTHER"))',
        f"os.environ['X_API_KEY'] = '{_MASKED}'",
        "os.environ['X_API_KEY'] = None",
        "os.environ['X_API_KEY'] = value",
        "os.environ['X_TOKEN'] = value or None",
        "os.environ['HOME'] = '/root'",
        "os.environ['USER'] = 'nobody'",
        'os.environ["X_API_KEY"] = os.getenv("Y")',
    ])
    def test_a_read_or_a_reference_stays_as_written(self, line):
        assert mask_api_key(line) == line

    @pytest.mark.parametrize("line, secret", [
        (f"os.environ['HF_TOKEN'] = 'hf_abcdefghijklmnopqrstuvwxyz012345'",
         "hf_abcdefghijklmnopqrstuvwxyz012345"),
        (f"os.environ['STRIPEKEY'] = 'sk-abcdefghijklmnopqrstuvwxyz012345'",
         "sk-abcdefghijklmnopqrstuvwxyz012345"),
    ])
    def test_a_provider_prefixed_value_is_still_masked(self, line, secret):
        assert secret not in mask_api_key(line)

    def test_a_masked_file_is_the_real_file_again(self):
        """A stand-in default is code in this repository's own sources: it stays, and the
        credentials around it come back when the model hands the file back."""
        source = (
            "class Settings:\n"
            f'    os.environ["STRIPEKEY"] = "{_HEX}"\n'
            f'    os.environ.setdefault("GITHUBTOKEN", "{_HEX}")\n'
            f'    System.setProperty("api.key", "{_HEX}")\n'
            '    os.environ.setdefault("CONTEST_GATE_API_KEY", "unset-for-the-round")\n'
            f'    os.environ["TOKENIZERS_PARALLELISM"] = "false"\n'
            f'    os.environ["PRIMARY_KEY"] = "{_HEX}"\n'
            "    timeout = 30\n"
        )
        masked = mask_api_key(source)
        # only `PRIMARY_KEY` keeps its value: it is a key column, not a credential
        assert masked.count(_HEX) == 1
        assert f'os.environ["PRIMARY_KEY"] = "{_HEX}"' in masked
        assert "unset-for-the-round" in masked
        assert masked.count(_MASKED) == 3
        assert mask_api_key(masked) == masked, "a masked value never re-masks"
        assert restore_masked_secrets(source, masked) == source


class TestBTheCommandLogReadsTheNameByItsWords:
    """Part B: `_SECRET_ENV_ASSIGN_RE` and the header rule ask the mask's helper."""

    @pytest.mark.parametrize("command, secret", [
        ("PGPASSWORD=hunter2 psql -c 'select 1'", "hunter2"),
        ("NGROK_AUTHTOKEN=gh1234567890 ngrok start", "gh1234567890"),
        ("GITHUBTOKEN=gh1234567890 git push", "gh1234567890"),
        ("DBPASSWORD=dbpass1234 make test", "dbpass1234"),
        ("SYSTEM_ACCESSTOKEN=abc123def456 tool run", "abc123def456"),
        ("SECRETKEY=abc123def456 tool run", "abc123def456"),
        ("DBPASSWD=dbpass1234 make test", "dbpass1234"),
        ("export PGPASSWORD=hunter2 && psql -c 'select 1'", "hunter2"),
        ("env AUTHTOKEN=tok123456789 tool run", "tok123456789"),
        ("; DBPASSWORD=dbpass1234 psql db", "dbpass1234"),
        ("npm config set //registry.npmjs.org/:_authToken=abc123def456", "abc123def456"),
        ("git config --global token.secret=AbCdEf123456", "AbCdEf123456"),
    ])
    def test_the_leaking_names_are_out_of_the_log(self, command, secret):
        out = _redact_command(command)
        assert secret not in out, command
        assert "[REDACTED]" in out, command

    @pytest.mark.parametrize("command", [
        "MAX_TOKENS=100 python x.py",
        "TOKENIZER=bert python x.py",
        "KEYBOARD=us python x.py",
        "python -m pytest --tokens=100 -q",
        "TOKENIZERS_PARALLELISM=false python -m pytest -q",
        "KEY_DIR=/tmp/keys ./run",
        "TOKEN_COUNT=5 python x.py",
        "FOO=bar pytest -q",
        "grep foo=bar file.txt",
        "env PATH=/usr/bin pytest -q",
        "python -m pytest tests/test_tokenizer.py::TestTokenizer::test_split -q",
        "npm view https://registry.npmjs.org:443/@types/node",
        "git clone https://host/path?query=value",
        "curl -H 'Content-Type: application/json' http://x",
    ])
    def test_the_fix40_table_stays_byte_for_byte(self, command):
        assert _redact_command(command) == command

    @pytest.mark.parametrize("command, expected", [
        ("OPENAI_API_KEY=sk123abc4567890 pytest", "OPENAI_API_KEY=[REDACTED] pytest"),
        ("export OPENAI_API_KEY=sk-abc123def456 && pytest", "export OPENAI_API_KEY=[REDACTED] && pytest"),
        ("API_KEY=\"a secret value\" pytest -q", "API_KEY=[REDACTED] pytest -q"),
        ("SECRET_PASSWD=abcd1234efgh python x.py", "SECRET_PASSWD=[REDACTED] python x.py"),
        ("curl -H 'x-api-key: abcd1234efgh5678' http://x", "curl -H 'x-api-key: [REDACTED]' http://x"),
        ("curl -H 'X-Auth-Token: abc123def' http://x", "curl -H 'X-Auth-Token: [REDACTED]' http://x"),
    ])
    def test_the_harder12d_table_stays(self, command, expected):
        assert _redact_command(command) == expected

    def test_redaction_never_raises(self):
        assert _redact_command("") == ""
        for command in (";", "&&", "|", "A=B", "--token=", "npm config set", "=", "a.b.c="):
            assert isinstance(_redact_command(command), str)


class TestCOneValuePatternForEveryFlag:
    """Part C: a quoted password with a space is one value, quotes included."""

    @pytest.mark.parametrize("command, expected", [
        ("mysql -p'my secret' mydb", "mysql -p[REDACTED] mydb"),
        ('mysql -p"my secret" mydb', "mysql -p[REDACTED] mydb"),
        ("redis-cli -a 'my secret' get key", "redis-cli -a [REDACTED] get key"),
        ('redis-cli -a "my secret" get key', "redis-cli -a [REDACTED] get key"),
        ("docker login -p 'my secret' registry.example", "docker login -p [REDACTED] registry.example"),
        ("mysql --password='my secret' mydb", "mysql --password=[REDACTED] mydb"),
        ('mysql --password="my secret" mydb', "mysql --password=[REDACTED] mydb"),
        ("curl -u 'admin:my secret' https://x.example", "curl -u 'admin:[REDACTED]' https://x.example"),
        ('curl -u "admin:my secret" https://x.example', 'curl -u "admin:[REDACTED]" https://x.example'),
        ("tool --token='my secret' run", "tool --token=[REDACTED] run"),
        ("tool --pass='my secret' run", "tool --pass=[REDACTED] run"),
        ("tool --passwd='my secret' run", "tool --passwd=[REDACTED] run"),
        ("tool --pwd='my secret' run", "tool --pwd=[REDACTED] run"),
        ("mysql --password='my secret' --other x", "mysql --password=[REDACTED] --other x"),
        ("curl -u \"a b:c d\" https://x.example", "curl -u \"a b:[REDACTED]\" https://x.example"),
    ])
    def test_the_whole_token_is_redacted(self, command, expected):
        assert _redact_command(command) == expected

    @pytest.mark.parametrize("command, expected", [
        ("mysql -p'my secret mydb", "mysql -p[REDACTED]"),
        ('mysql --password="my secret mydb', "mysql --password=[REDACTED]"),
        ("curl -u 'admin:my secret https://x.example", "curl -u 'admin:[REDACTED]"),
    ])
    def test_an_unterminated_quote_runs_to_the_end_of_the_line(self, command, expected):
        assert _redact_command(command) == expected

    @pytest.mark.parametrize("command", [
        "ls -p",
        "grep -p pat file.txt",
        "pytest -p no:cacheprovider",
        "docker run -p 8080:80 nginx",
        "redis-cli -h localhost -p 6380 ping",
        "docker login --password-stdin",
        "docker run -u 1000:1000 img",
        "docker run -u '1000:1000' img",
        "tool --token=",
    ])
    def test_a_command_without_a_secret_is_byte_for_byte(self, command):
        assert _redact_command(command) == command

    @pytest.mark.parametrize("command, secret", [
        ("mysql -p mydb", "mydb"),
        ("mysqldump -p mydb", "mydb"),
        ("mysql -p my_database", "my_database"),
        ("mysql -p db_orders mydb", "db_orders"),
        ("mysql -p5432", "5432"),
    ])
    def test_the_database_exception_is_only_for_the_unquoted_word(self, command, secret):
        """`mysql -p mydb` keeps its database: `-p` alone is the prompt flag. The
        exception does not reach a quoted word, which is a password whatever it holds."""
        assert _redact_command(command) == command

    @pytest.mark.parametrize("command, expected", [
        ("mysql -p'mydb' mydb", "mysql -p[REDACTED] mydb"),
        ('mysql -p"mydb" mydb', "mysql -p[REDACTED] mydb"),
    ])
    def test_a_quoted_word_is_a_password(self, command, expected):
        assert _redact_command(command) == expected

    @pytest.mark.parametrize("command, secret", [
        ("mysql -psecretpass1234 db", "secretpass1234"),
        ("mysql -psecretpass1234", "secretpass1234"),
        ("mysql -p secretpass1234 db", "secretpass1234"),
        ("mysql -u root -p SuperSecret123 -h db.internal", "SuperSecret123"),
        ("redis-cli -a SuperSecret123 get key", "SuperSecret123"),
        ("docker login -p secretpass1234", "secretpass1234"),
        ("docker login --password hunter2secret", "hunter2secret"),
        ("docker login --password=hunter2secret", "hunter2secret"),
        ("curl -u admin:Passw0rd https://x.example", "Passw0rd"),
        ("tool --token=abc123", "abc123"),
    ])
    def test_the_unquoted_forms_still_redact(self, command, secret):
        out = _redact_command(command)
        assert secret not in out, command
        assert "[REDACTED]" in out, command

    def test_a_secret_survives_no_form(self):
        for secret in ("my secret", "secretpass1234", "Passw0rd", "hunter2secret"):
            for command in (
                f"mysql -p'{secret}' mydb",
                f'mysql --password="{secret}" mydb',
                f"redis-cli -a '{secret}' get key",
                f"curl -u admin:{secret} https://x.example",
                f"PGPASSWORD={secret} psql -c 'select 1'",
            ):
                out = _redact_command(command)
                assert secret not in out, command
                assert "[REDACTED]" in out, command
