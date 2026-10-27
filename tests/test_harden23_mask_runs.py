"""HARDEN-23 C (own finds): mask_api_key stays linear on a long run of blanks, repeated `apikey`, `env.` or `os.environ.`."""
import time

import pytest

from tools.llm_stream import mask_api_key

N = 20000
LIMIT_SEC = 3.0

RUNS = {
    "spaces": " " * N,
    "tabs": "\t" * N,
    "spaces then key": " " * N + "api_key = 'abc'",
    "hash then spaces": "# " + " " * N,
    "apikey repeated": "apikey" * (N // 6),
    "api_key repeated": "api_key" * (N // 7),
    "env dot repeated": "env." * (N // 4),
    "os.environ dot repeated": "os.environ." * (N // 11),
    "xml attrs": "<a " + "k=v " * (N // 4) + ">",
    "xml attr name run": "<a " + "k" * N + "=v>",
}


@pytest.mark.parametrize("name", sorted(RUNS))
def test_a_long_run_is_masked_in_linear_time(name):
    started = time.perf_counter()
    mask_api_key(RUNS[name])
    assert time.perf_counter() - started < LIMIT_SEC


@pytest.mark.parametrize("text", [
    '  api_key = "sk-abcdef1234567890abcdef"',
    "\t\tOPENAI_API_KEY: sk-abcdef1234567890",
    "# ; API_KEY = abcdef123456",
    'process.env.STRIPE_API_KEY = "abcdef123456"',
    "os.environ.API_KEY=abcdef123456",
    "a.env.my-token = zzzzzzzz1234",
    '<input api_key="abcdef123456">',
    "api_key=abcdef123456 api_key=zzzzzz987654",
    "A_API_KEY=abcdef123456 \tB_API_KEY=zzzzzz987654",
])
def test_the_credential_after_blanks_or_a_property_path_is_still_masked(text):
    assert "abcdef1234" not in mask_api_key(text) and "zzzzzzzz" not in mask_api_key(text)


def test_a_credential_behind_a_long_run_of_blanks_is_masked():
    text = " " * N + "\napi_key = 'sk-abcdef1234567890abcdef'"
    assert "sk-abcdef" not in mask_api_key(text)
