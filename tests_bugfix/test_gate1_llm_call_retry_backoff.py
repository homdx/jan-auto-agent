"""tests/test_gate1_llm_call_retry_backoff.py — AUTO-RETRY-BACKOFF-1.

Covers the OUTER retry — a plain LLM-call EXCEPTION (network error, an
HTTP error that surfaced as an exception) in `_check_presence`, distinct
from the "unparseable verdict" retry (see
test_gate1_unparseable_retry_escalation.py), which only fires once a
response was actually RECEIVED and failed to parse.

Field report: a real HTTP 400 from a provider ("api.ai") outlasted the
previous single, immediate (no-wait) retry — every candidate hit during
the outage window failed closed and, before AUTO-REMOVE-GUARD-1 (see
tests/test_auto_h1.py's TestValidatePlanTechnicalFailureHandling), would
have been permanently deleted from plan.json as an indistinguishable
"already fixed" false positive.

Fix: configurable retry count (`[gate1] llm_call_retry_max`, default 3)
with a REAL pause (`[gate1] llm_call_retry_wait_sec`, default 60s)
between attempts, giving a provider outage a genuine window to clear.
"""

from __future__ import annotations

import configparser
import sys
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import tools.llm_stream as llm_stream_mod
from tools.auto.architect import CandidateTask, CitedLocation
from tools.auto.gate1_filter import Gate1Filter, _is_technical_failure


@pytest.fixture(autouse=True)
def _reset_caches():
    llm_stream_mod._REASONING_UNSUPPORTED_KEYS.clear()
    yield
    llm_stream_mod._REASONING_UNSUPPORTED_KEYS.clear()


def _make_filter(*, llm_call_retry_max=None, llm_call_retry_wait_sec=None) -> Gate1Filter:
    cfg = configparser.ConfigParser()
    gate1 = {"temperature": "0.0", "max_tokens": "512", "skip_llm": "false"}
    if llm_call_retry_max is not None:
        gate1["llm_call_retry_max"] = str(llm_call_retry_max)
    if llm_call_retry_wait_sec is not None:
        gate1["llm_call_retry_wait_sec"] = str(llm_call_retry_wait_sec)
    cfg.read_dict({
        "api":       {"active": "local", "verify_ssl": "false"},
        "api_local": {
            "base_url":   "http://localhost:1337/v1",
            "api_key":    "test",
            "model":      "test-model",
            "api_format": "openai",
        },
        "gate1": gate1,
        "loop":  {"timeout_seconds": "10"},
    })
    return Gate1Filter(
        config=cfg, base_url="http://localhost:1337/v1",
        api_key="test", model="test-model", api_format="openai", verify_ssl=False,
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "utils.py").write_text(
        textwrap.dedent("""\
            def parse_config(raw):
                return raw
        """),
        encoding="utf-8",
    )
    return tmp_path


def _candidate() -> CandidateTask:
    return CandidateTask(
        title="Add input validation", instruction="validate that raw is a dict",
        target_files=["tools/utils.py"], acceptance_check="true",
        cited_location=CitedLocation(file="tools/utils.py", symbol="parse_config"),
    )


class TestConfigDefaultsAndOverrides:

    def test_default_retry_max_is_three(self):
        filt = _make_filter()
        assert filt._llm_call_retry_max == 3

    def test_default_wait_sec_is_sixty(self):
        filt = _make_filter()
        assert filt._llm_call_retry_wait_sec == pytest.approx(60.0)

    def test_custom_values_respected(self):
        filt = _make_filter(llm_call_retry_max=5, llm_call_retry_wait_sec=15)
        assert filt._llm_call_retry_max == 5
        assert filt._llm_call_retry_wait_sec == pytest.approx(15.0)

    def test_negative_retry_max_clamped_to_zero(self):
        filt = _make_filter(llm_call_retry_max=-2)
        assert filt._llm_call_retry_max == 0

    def test_negative_wait_sec_clamped_to_zero(self):
        filt = _make_filter(llm_call_retry_wait_sec=-10)
        assert filt._llm_call_retry_wait_sec == pytest.approx(0.0)


class TestRetryBehaviorOnPersistentFailure:
    """request_completion raises on EVERY call — the provider never
    recovers within the retry budget."""

    def test_attempts_max_plus_one_calls(self, repo):
        filt = _make_filter(llm_call_retry_max=3, llm_call_retry_wait_sec=60)
        with patch("tools.auto.gate1_filter.time.sleep") as mock_sleep, \
             patch("tools.llm_stream.request_completion",
                   side_effect=RuntimeError("HTTP 400 from https://provider.example")) as mock_llm:
            accepted, rejected = filt.filter([_candidate()], repo)

        assert mock_llm.call_count == 4  # 1 initial + 3 retries
        assert mock_sleep.call_count == 3  # once before each retry, none before the first

    def test_sleeps_the_configured_wait_seconds_every_time(self, repo):
        filt = _make_filter(llm_call_retry_max=3, llm_call_retry_wait_sec=45)
        with patch("tools.auto.gate1_filter.time.sleep") as mock_sleep, \
             patch("tools.llm_stream.request_completion",
                   side_effect=RuntimeError("boom")):
            filt.filter([_candidate()], repo)

        assert [c.args[0] for c in mock_sleep.call_args_list] == [45.0, 45.0, 45.0]

    def test_result_is_rejected_with_technical_reason(self, repo):
        filt = _make_filter(llm_call_retry_max=2, llm_call_retry_wait_sec=0)
        with patch("tools.auto.gate1_filter.time.sleep"), \
             patch("tools.llm_stream.request_completion",
                   side_effect=RuntimeError("HTTP 400 from https://provider.example")):
            accepted, rejected = filt.filter([_candidate()], repo)

        assert accepted == []
        assert len(rejected) == 1
        assert rejected[0].reason.startswith("LLM call failed:")
        assert "(after 2 retries)" in rejected[0].reason
        assert "HTTP 400" in rejected[0].reason

    def test_reason_is_classified_as_technical_failure(self, repo):
        """The whole point of AUTO-REMOVE-GUARD-1 (plan_validator.py)
        depends on this classification staying correct after the reason
        string's format changed from "(after 1 retry)" to
        "(after N retries)"."""
        filt = _make_filter(llm_call_retry_max=1, llm_call_retry_wait_sec=0)
        with patch("tools.auto.gate1_filter.time.sleep"), \
             patch("tools.llm_stream.request_completion",
                   side_effect=RuntimeError("boom")):
            accepted, rejected = filt.filter([_candidate()], repo)

        assert _is_technical_failure(rejected[0].reason) is True


class TestRetryBehaviorOnRecovery:
    """request_completion recovers partway through the retry budget."""

    def test_succeeds_on_a_later_attempt_stops_retrying(self, repo):
        filt = _make_filter(llm_call_retry_max=3, llm_call_retry_wait_sec=0)
        good_verdict = '{"verdict": "confirmed", "evidence": "return raw", "reason": "x"}'
        side_effects = [RuntimeError("boom"), RuntimeError("boom"), good_verdict]
        with patch("tools.auto.gate1_filter.time.sleep") as mock_sleep, \
             patch("tools.llm_stream.request_completion", side_effect=side_effects) as mock_llm:
            accepted, rejected = filt.filter([_candidate()], repo)

        assert mock_llm.call_count == 3  # stopped as soon as it succeeded
        assert mock_sleep.call_count == 2  # only slept before the 2 failed retries
        assert len(accepted) == 1
        assert rejected == []

    def test_succeeds_on_the_very_first_attempt_never_sleeps(self, repo):
        filt = _make_filter()
        good_verdict = '{"verdict": "confirmed", "evidence": "return raw", "reason": "x"}'
        with patch("tools.auto.gate1_filter.time.sleep") as mock_sleep, \
             patch("tools.llm_stream.request_completion", return_value=good_verdict) as mock_llm:
            filt.filter([_candidate()], repo)

        assert mock_llm.call_count == 1
        mock_sleep.assert_not_called()


class TestRetryMaxZeroDisablesRetry:

    def test_single_attempt_no_sleep(self, repo):
        filt = _make_filter(llm_call_retry_max=0, llm_call_retry_wait_sec=60)
        with patch("tools.auto.gate1_filter.time.sleep") as mock_sleep, \
             patch("tools.llm_stream.request_completion",
                   side_effect=RuntimeError("boom")) as mock_llm:
            accepted, rejected = filt.filter([_candidate()], repo)

        assert mock_llm.call_count == 1
        mock_sleep.assert_not_called()
        assert "(after 0 retries)" in rejected[0].reason
