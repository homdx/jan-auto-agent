"""HARDEN-19 judging gaps: the attribute form `process.env.NAME = …` reads a name by its words, and a database key column is not a credential."""
import pytest

from tools.llm_stream import _name_is_a_credential, mask_api_key

HEX = "9f3a1c5e7b2d4a6c8e0f1a3b5c7d9e2f"


@pytest.mark.parametrize("line", [
    'process.env.STRIPEKEY = "%s"',
    'process.env.clientSecret = "%s";',
    'Bun.env.GITHUBTOKEN = "%s"',
    'process.env.API_KEY = "%s"',
])
def test_attribute_form_of_an_env_write_is_masked(line):
    out = mask_api_key(line % HEX)
    assert HEX not in out
    assert "here_your_key" in out


@pytest.mark.parametrize("line", [
    'process.env.MAX_TOKENS = "%s"',
    'process.env.HOME = "/root"',
    'dotenv.STRIPEKEY = "%s"',
    'if (process.env.STRIPEKEY == "%s") {',
    'process.env.STRIPEKEY = process.env.OTHER',
])
def test_attribute_form_leaves_what_is_not_an_env_credential_write(line):
    text = line % HEX if "%s" in line else line
    assert mask_api_key(text) == text


@pytest.mark.parametrize("name", ["PRIMARY_KEY", "FOREIGN_KEY", "UNIQUE_KEY", "INDEX_KEY", "SORT_KEY", "user_primary_key"])
def test_a_database_key_column_is_not_a_credential(name):
    assert not _name_is_a_credential(name)
    text = 'os.environ["%s"] = "%s"' % (name, HEX)
    assert mask_api_key(text) == text


@pytest.mark.parametrize("name", ["STRIPE_KEY", "API_KEY", "STRIPEKEY", "secretKey"])
def test_a_key_name_that_is_not_a_database_kind_still_is(name):
    assert _name_is_a_credential(name)


def test_npm_config_set_with_a_space_redacts_the_token_value():
    from tools.auto.executor import _redact_command
    out = _redact_command("npm config set //registry.npmjs.org/:_authToken ghp_abcdefghijklmnopqrstuvwxyz0123")
    assert "ghp_abcdefghij" not in out and out.endswith("[REDACTED]")
    assert _redact_command("npm config set registry https://r.example") == "npm config set registry https://r.example"
