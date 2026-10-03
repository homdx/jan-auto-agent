"""Round 149 follow-up: three holes the Sonnet 4/5 review patches of 147 found in the overflow reading."""
import math
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_contest_context_memory as ctm  # noqa: E402
import test_contest_runner as tr  # noqa: E402
from tools.contest import context_memory as cm  # noqa: E402
from tools.contest import runner as rm  # noqa: E402


def _api(message, status=400):
    return {"name": "APIError",
            "data": {"message": message, "statusCode": status, "isRetryable": False}}


@pytest.mark.parametrize("message", [
    "blocked by content policy",
    "Request flagged by moderation",
    "Unexpected end of JSON input",
    "Invalid tool call arguments",
    "Invalid schema for function 'bash': unexpected property",
])
def test_a_400_that_names_a_request_fault_is_not_a_full_refusal(message):
    """A full session refused for its JSON, a tool call or a safety filter has not
    hit its window, and must not teach the memory one."""
    assert not rm._is_full_refusal(_api(message), 80_000, 98_777)


@pytest.mark.parametrize("message", [
    "x",
    "Input validation error: inputs tokens + max_new_tokens must be <= 4096",
])
def test_a_wordless_or_token_counting_400_still_reads_as_full(message):
    assert rm._is_full_refusal(_api(message), 80_000, 98_777)


def test_a_status_code_inside_a_request_id_is_no_retry():
    error = _api("prompt is too long: 210000 tokens > 200000 maximum, request id req_429ab503")
    assert not rm._retryable(error)
    assert rm._is_overflow(error)


@pytest.mark.parametrize("message", ["HTTP 429.", "status 503: busy", "502 Bad Gateway",
                                     "error 504"])
def test_a_status_code_standing_alone_is_still_a_retry(message):
    assert rm._retryable(_api(message))


@pytest.mark.parametrize("value", [math.inf, -math.inf, math.nan])
def test_min_window_never_raises_on_a_non_finite_number(value):
    assert cm.min_window(SimpleNamespace(context_min_window=value)) == cm.DEFAULT_MIN_WINDOW


def test_max_tokens_in_a_context_limit_sum_is_still_an_overflow():
    """The output cap is a term of the sum there, not a refusal of the cap."""
    assert rm._is_overflow(_api(
        "input length and `max_tokens` exceed context limit: 188240 + 21333 > 200000"))
    assert not rm._is_overflow(_api("max_tokens exceeds the model limit"))


@pytest.mark.parametrize("status", [429, 503])
def test_size_words_on_a_rate_or_provider_status_are_no_overflow(status):
    assert not rm._is_overflow(_api("Prompt exceeds max length", status))


def test_a_declared_32k_window_refused_at_28k_is_an_overflow(tmp_path):
    """Round 149's lowered floor holds in the runner too: Kilo declares 32 768,
    28 000 went through, the next request was refused — full, not a plan's cap."""
    memory = tmp_path / "context-memory.json"
    config = ctm._config(tmp_path, memory=memory, max_continues_per_attempt=0)
    config = replace(config, agents=tuple(replace(a, context_limit=32_768)
                                          for a in config.agents))
    scenario = ctm._overflow_scenario(_api("Prompt exceeds max length"), 28_000)
    _sb, _fake, _h, run, _ = tr._run_one(tmp_path, scenario, config)
    assert run.state is tr.AgentState.STALLED and run.last_error == "context overflow"
    assert cm.load(memory)[0].last_ok == 28_000
