"""HARDEN-15 (313): judge-found cases — a base64 value keeps no padding, a lower-case `sk-` run with one digit is a key, a URL tail survives."""
import pytest

from tools.auto.executor import _redact_command
from tools.llm_stream import mask_api_key, restore_masked_secrets


def test_a_padded_base64_value_loses_its_padding_too():
    assert mask_api_key("api_key=QWxhZGRpbjpvcGVuIHNlc2FtZQ==\n") == "api_key=here_your_key\n"


@pytest.mark.parametrize("line", [
    "sk-live-aaaaaaaaaaaa1\n",
    "OPENAI_API_KEY=sk-AbCdEf1234567890\n",
])
def test_a_lower_case_run_with_a_digit_is_a_key(line):
    out = mask_api_key(line)
    assert "here_your_key" in out and "aaaaaaaaaaaa1" not in out and "AbCdEf1234567890" not in out


@pytest.mark.parametrize("line", [
    "x = \"it's ?api_key=\" + k + \"&a=1\"\n",
    "u = `https://a/?api_key=${key}&x=1`\n",
    'url = f"https://a/?api_key={key}&b=2"\n',
    "sk-fading-circle-2 sk-learn-pipeline-1\n",
])
def test_code_with_a_key_name_inside_a_string_is_left_alone(line):
    assert mask_api_key(line) == line


def test_a_quoted_url_literal_is_masked_and_its_tail_kept():
    out = mask_api_key('u = "https://a/?api_key=abc123def456ghi&x=1" # c\n')
    assert "abc123def456ghi" not in out and "&x=1" in out and out.endswith("# c\n")


def test_a_faithful_edit_of_the_url_line_round_trips():
    line = 'url = "https://a.com/d?api_key=" + key + "&format=json"\n'
    shown = mask_api_key(line)
    assert restore_masked_secrets(line, shown.replace("json", "xml")) == line.replace("json", "xml")


@pytest.mark.parametrize("cmd", [
    "redis-cli -u redis://host:6379 ping", "redis-cli -u redis://host ping",
    "ls -a /tmp", "mysql -p mydb -e 'select 1'", "mysqldump -p mydb > out.sql",
])
def test_a_command_with_no_secret_is_logged_as_written(cmd):
    assert _redact_command(cmd) == cmd
