"""Review q: a name that only contains a credential word is not a credential, in the mask and in the command log.

CC review fix 40 made the credential word a whole part of the name, so `TOKENIZERS_PARALLELISM`
and `KEYBOARD_LAYOUT` are not credentials. HARDEN-19 found that it went too far: `STRIPEKEY`,
`GITHUBTOKEN`, `AUTHTOKEN`, `clientSecret` and `dbPassword` name credentials too, and the
"whole part" rule threw them away. The name rule is now one helper both sides ask, and it
reads the name by its words — a credential word counts either as a whole word or as the
name's tail written together, with a stoplist of false friends. The fix-40 cases below stay
green on both sides, and so do the cases it broke.
"""

from __future__ import annotations

import pytest

from tools.auto.executor import _redact_command
from tools.llm_stream import mask_api_key


_HEX = "0123456789abcdef0123456789abcdef"


@pytest.mark.parametrize("line", [
    'os.environ["TOKENIZERS_PARALLELISM"] = "false"',
    'os.environ["KEYBOARD_LAYOUT"] = "us"',
    'os.environ["MONKEYPATCH_ENABLED"] = "1"',
    'os.environ["PRIMARY_KEY"] = "0123456789abcdef0123456789abcdef"',
    'os.environ["MAX_TOKENS"] = "0123456789abcdef0123456789abcdef"',
    'os.environ["MONKEY_PATCH"] = "0123456789abcdef0123456789abcdef"',
    'os.environ["KEYMAP"] = "0123456789abcdef0123456789abcdef"',
    'os.environ.setdefault("KEYBOARD_LAYOUT", "us")',
    'os.environ["TOKEN_COUNT"] = "5"',
    'os.environ["KEY_DIR"] = "/tmp/keys"',
    'apiKey = "apiKey"',
    'api_key = "api_key"',
    '"api_key": "",',
    '    "openai_api_key": "",  # OpenAI API Key',
])
def test_code_that_holds_no_key_is_sent_as_written(line):
    assert mask_api_key(line) == line


@pytest.mark.parametrize("line", [
    'os.environ["SECRET_KEY"] = "dev-only-secret-123"',
    'os.environ["GROQ_API_KEY"] = "gsk_abcdefghijklmnopqrstuvwxyz0123"',
    'os.environ["OPENAI_APIKEY"] = "AbCdEf0123456789xyz"',
    "os.environ['HF_TOKEN'] = 'AbCdEf0123456789xyz'",
    'api_key = "AbCdEf0123456789xyz"',
    '"api_key": "AbCdEf0123456789xyz",',
])
def test_a_key_is_still_masked(line):
    masked = mask_api_key(line)
    assert "here_your_key" in masked and "AbCdEf0123456789xyz" not in masked and "dev-only" not in masked


@pytest.mark.parametrize("name", [
    "STRIPEKEY", "GITHUBTOKEN", "SYSTEM_ACCESSTOKEN", "AUTHTOKEN", "SECRETKEY",
    "OpenAIKey", "GitHubToken", "clientSecret", "dbPassword",
    "PGPASSWORD", "DBPASSWORD", "MY_PASSWD", "client_secret", "access_key",
])
def test_a_credential_name_written_as_one_word_or_in_camelcase_is_masked(name):
    """The 24 names fix 40 threw away: the credential word is the name's tail, written
    together, and it still names a credential."""
    line = f'os.environ["{name}"] = "{_HEX}"'
    assert mask_api_key(line) == f'os.environ["{name}"] = "here_your_key"'


@pytest.mark.parametrize("name", [
    "TOKENIZERS_PARALLELISM", "KEYBOARD_LAYOUT", "MONKEY_PATCH", "MAX_TOKENS",
    "PRIMARY_KEY", "KEYMAP", "MONKEY", "TURKEY", "HOTKEY", "DONKEY",
])
def test_a_false_friend_is_not_a_credential_in_any_form(name):
    """The stoplist: `MONKEY`, `TURKEY`, `HOTKEY` and `DONKEY` end in `key`, `KEYBOARD`
    starts with one, and none of them holds a credential."""
    for line in (f'os.environ["{name}"] = "{_HEX}"',
                 f'os.environ.setdefault("{name}", "{_HEX}")',
                 f'process.env["{name}"] = "{_HEX}"',
                 f'ENV["{name}"] = "{_HEX}"'):
        assert mask_api_key(line) == line, line


@pytest.mark.parametrize("template", [
    'os.environ["{name}"] = "{value}"',
    'os.environ.setdefault("{name}", "{value}")',
    'process.env["{name}"] = "{value}"',
    'ENV["{name}"] = "{value}"',
    '$_ENV["{name}"] = "{value}"',
])
def test_the_access_forms_of_one_name_mask_alike(template):
    """The five access forms of the same name are masked alike, whatever the language."""
    for name in ("STRIPEKEY", "PGPASSWORD"):
        line = template.format(name=name, value=_HEX)
        assert mask_api_key(line) == template.format(name=name, value="here_your_key"), line


@pytest.mark.parametrize("command", [
    "python -m pytest tests/test_tokenizer.py::TestTokenizer::test_split -q",
    "python -m pytest tests/test_auth.py::TestApiKey::test_rotation -q",
    "TOKENIZERS_PARALLELISM=false python -m pytest -q",
    "npm view https://registry.npmjs.org:443/@types/node",
    "docker run -u 1000:1000 img pytest -q",
    "MAX_TOKENS=100 python x.py",
    "TOKENIZER=bert python x.py",
    "KEYBOARD=us python x.py",
    "python -m pytest --tokens=100 -q",
])
def test_a_command_without_a_secret_is_logged_byte_for_byte(command):
    assert _redact_command(command) == command


@pytest.mark.parametrize("command, secret", [
    ("GROQ_API_KEY=gsk_abc123def456 pytest -q", "gsk_abc123def456"),
    ("export OPENAI_API_KEY=sk-abc123def456 && pytest", "sk-abc123def456"),
    ("curl -H 'x-api-key: abc123def' https://x", "abc123def"),
    ("curl -H 'X-Auth-Token: abc123def' https://x", "abc123def"),
    ("tool --token=abc123", "abc123"),
    ("git clone https://user:ghp_abcdefghijklmnopqrstuvwxyz0123@github.com/a/b.git", "ghp_abcdefghij"),
    ("curl -u alice:s3cretpw https://x", "s3cretpw"),
    ("PGPASSWORD=hunter2 psql -c 'select 1'", "hunter2"),
    ("NGROK_AUTHTOKEN=tok1234567890 ngrok start", "tok1234567890"),
    ("GITHUBTOKEN=gh1234567890 git push", "gh1234567890"),
    ("DBPASSWORD=dbpass1234 make test", "dbpass1234"),
    ("npm config set //registry.npmjs.org/:_authToken=abc123def456", "abc123def456"),
])
def test_a_secret_in_a_command_is_still_redacted(command, secret):
    logged = _redact_command(command)
    assert secret not in logged and "[REDACTED]" in logged
