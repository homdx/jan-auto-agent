"""Review q: a name that only contains a credential word is not a credential, in the mask and in the command log."""

from __future__ import annotations

import pytest

from tools.auto.executor import _redact_command
from tools.llm_stream import mask_api_key


@pytest.mark.parametrize("line", [
    'os.environ["TOKENIZERS_PARALLELISM"] = "false"',
    'os.environ["KEYBOARD_LAYOUT"] = "us"',
    'os.environ["MONKEYPATCH_ENABLED"] = "1"',
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


@pytest.mark.parametrize("command", [
    "python -m pytest tests/test_tokenizer.py::TestTokenizer::test_split -q",
    "python -m pytest tests/test_auth.py::TestApiKey::test_rotation -q",
    "TOKENIZERS_PARALLELISM=false python -m pytest -q",
    "npm view https://registry.npmjs.org:443/@types/node",
    "docker run -u 1000:1000 img pytest -q",
])
def test_a_command_without_a_secret_is_logged_byte_for_byte(command):
    assert _redact_command(command) == command


@pytest.mark.parametrize("command,secret", [
    ("GROQ_API_KEY=gsk_abc123def456 pytest -q", "gsk_abc123def456"),
    ("export OPENAI_API_KEY=sk-abc123def456 && pytest", "sk-abc123def456"),
    ("curl -H 'x-api-key: abc123def' https://x", "abc123def"),
    ("curl -H 'X-Auth-Token: abc123def' https://x", "abc123def"),
    ("tool --token=abc123", "abc123"),
    ("git clone https://user:ghp_abcdefghijklmnopqrstuvwxyz0123@github.com/a/b.git", "ghp_abcdefghij"),
    ("curl -u alice:s3cretpw https://x", "s3cretpw"),
])
def test_a_secret_in_a_command_is_still_redacted(command, secret):
    logged = _redact_command(command)
    assert secret not in logged and "[REDACTED]" in logged
