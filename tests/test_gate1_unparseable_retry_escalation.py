"""tests/test_gate1_unparseable_retry_escalation.py — AUTO-RETRY-TEMP-1.

Field report: with a [gate1]/[gate1_llm] temperature=0.0 base, the OLD
escalation formula (`base_temp - attempt * STEP`, floored at 0.0)
computed 0.0 for EVERY single retry attempt — subtracting from an
already-zero base never leaves the floor. A real run confirmed the
consequence directly: 5 consecutive retries against the same candidate
produced byte-for-byte IDENTICAL broken JSON (the same unescaped-quote
mistake at the same position) every time, because at temperature=0.0 the
model is deterministic and a wider max_tokens budget alone doesn't
retroactively change tokens the model already committed to earlier in
the same greedy decode — only a genuinely different sampling temperature
can escape a repeated deterministic mistake like that.

Fix: retry-recovery mode now uses its own fixed, ABSOLUTE temperature
schedule (0.0, 0.1 by default), decoupled entirely from whatever the
happy-path base temperature is configured to. Every temperature in the
schedule is tried at the CURRENT token tier before the tier doubles —
e.g. with the default 5 retries: (4096, 0.0), (4096, 0.1), (8192, 0.0),
(8192, 0.1), (16384, 0.0).
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
from tools.auto.gate1_filter import Gate1Filter


@pytest.fixture(autouse=True)
def _reset_caches():
    """Process-lifetime global state (tools.llm_stream) — reset around
    every test so tests in this file can't leak into each other or into
    other test files, regardless of run order."""
    llm_stream_mod._REASONING_UNSUPPORTED_KEYS.clear()
    yield
    llm_stream_mod._REASONING_UNSUPPORTED_KEYS.clear()


def _make_filter(*, gate1_temperature="0.0", gate1_max_tokens="512",
                  num_ctx=None) -> Gate1Filter:
    cfg = configparser.ConfigParser()
    api_local = {
        "base_url":   "http://localhost:1337/v1",
        "api_key":    "test",
        "model":      "test-model",
        "api_format": "openai",
    }
    if num_ctx is not None:
        api_local["num_ctx"] = str(num_ctx)
    cfg.read_dict({
        "api":       {"active": "local", "verify_ssl": "false"},
        "api_local": api_local,
        "gate1": {
            "temperature": gate1_temperature,
            "max_tokens":  gate1_max_tokens,
            "skip_llm":    "false",
        },
        "loop": {"timeout_seconds": "10"},
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


def _run_and_capture_payloads(filt: Gate1Filter, repo: Path,
                               return_value: str = "") -> list[dict]:
    """Runs filt.filter() against one candidate with request_completion
    mocked to always return *return_value* (unparseable by default —
    empty string), capturing every outgoing payload's max_tokens/
    temperature via a build_chat_request wrapper that still calls
    through to the real implementation."""
    real_build = llm_stream_mod.build_chat_request
    captured: list[dict] = []

    def _capture(**kwargs):
        result = real_build(**kwargs)
        captured.append(result[2])  # payload
        return result

    with patch("tools.llm_stream.build_chat_request", side_effect=_capture), \
         patch("tools.llm_stream.request_completion", return_value=return_value):
        filt.filter([_candidate()], repo)

    return captured


class TestTokenTemperatureEscalationSchedule:

    def test_full_default_schedule(self, repo):
        filt = _make_filter(gate1_temperature="0.0", gate1_max_tokens="512")
        payloads = _run_and_capture_payloads(filt, repo)

        # 1 initial (unescalated) call + 6 retries (default max).
        assert len(payloads) == 7
        assert payloads[0]["max_tokens"] == 512     # initial: base config
        assert payloads[0]["temperature"] == pytest.approx(0.0)  # initial: base config

        expected = [
            (4096, 0.0),   # retry 1: tier 0
            (4096, 0.1),   # retry 2: tier 0, 2nd temp
            (8192, 0.0),   # retry 3: tier 1
            (8192, 0.1),   # retry 4: tier 1, 2nd temp
            (16384, 0.0),  # retry 5: tier 2
            (16384, 0.1),  # retry 6: tier 2, 2nd temp
        ]
        for i, (tok, temp) in enumerate(expected, start=1):
            assert payloads[i]["max_tokens"] == tok, f"retry {i} max_tokens"
            assert payloads[i]["temperature"] == pytest.approx(temp), f"retry {i} temperature"

    def test_temperature_varies_even_when_base_is_already_zero(self, repo):
        """The exact bug this replaces: a [gate1] temperature=0.0 base
        must NOT collapse every retry's temperature to 0.0 — at least
        one retry must use a non-zero value, or a deterministic model
        stuck on the same mistake never gets a chance to produce
        anything different."""
        filt = _make_filter(gate1_temperature="0.0")
        payloads = _run_and_capture_payloads(filt, repo)

        retry_temps = [p["temperature"] for p in payloads[1:]]
        assert any(t > 0.0 for t in retry_temps), (
            "every retry used temperature=0.0 — identical to the base "
            "config, meaning a deterministic failure can never be "
            "escaped by retrying"
        )
        assert retry_temps == [0.0, 0.1, 0.0, 0.1, 0.0, 0.1]

    def test_temperature_schedule_ignores_a_nonzero_base_temperature(self, repo):
        """The fixed (0.0, 0.1) schedule is deliberately NOT computed
        relative to the base config's temperature — a [gate1]
        temperature=0.4 base must produce the exact same retry schedule
        as a temperature=0.0 base, not (0.4-0.1, 0.4-0.2, ...)."""
        filt = _make_filter(gate1_temperature="0.4")
        payloads = _run_and_capture_payloads(filt, repo)

        assert payloads[0]["temperature"] == pytest.approx(0.4)  # initial: base config
        retry_temps = [p["temperature"] for p in payloads[1:]]
        assert retry_temps == [0.0, 0.1, 0.0, 0.1, 0.0, 0.1]

    def test_token_tier_doubles_only_every_two_retries(self, repo):
        filt = _make_filter()
        payloads = _run_and_capture_payloads(filt, repo)

        retry_tokens = [p["max_tokens"] for p in payloads[1:]]
        assert retry_tokens == [4096, 4096, 8192, 8192, 16384, 16384]

    def test_last_tier_gets_both_temperatures(self, repo):
        """AUTO-RETRY-TEMP-1: _UNPARSEABLE_MAX_RETRIES=6 is deliberately
        an even multiple of len(_UNPARSEABLE_TEMPERATURES)=2, so the LAST
        tier the ladder reaches also gets both temperatures tried — not
        just the first one, which an odd retry count would leave
        untried."""
        filt = _make_filter()
        payloads = _run_and_capture_payloads(filt, repo)

        last_tier_payloads = payloads[-2:]
        assert [p["max_tokens"] for p in last_tier_payloads] == [16384, 16384]
        assert [p["temperature"] for p in last_tier_payloads] == [
            pytest.approx(0.0), pytest.approx(0.1),
        ]

    def test_ceiling_tracks_presence_num_ctx_when_configured(self, repo):
        """Ceiling = num_ctx * 0.5 — matches ClusterReviewer's own
        ctx-aware ceiling for the same reason: don't request more
        tokens than the model's real context window on a small-context
        setup."""
        filt = _make_filter(num_ctx=20000)
        assert filt._presence_num_ctx == 20000
        payloads = _run_and_capture_payloads(filt, repo)

        retry_tokens = [p["max_tokens"] for p in payloads[1:]]
        # ceiling = int(20000 * 0.5) = 10000; tier 2 (16384) gets capped.
        assert retry_tokens == [4096, 4096, 8192, 8192, 10000, 10000]

    def test_ceiling_defaults_to_flat_value_when_num_ctx_unset(self, repo):
        """No num_ctx configured at all — falls back to the flat 32768
        default, not 0 (which would collapse every tier to the floor).
        Default 6 retries never reach tier 3 (32768) — bump the retry
        budget via a custom filter to actually exercise the cap."""
        filt = _make_filter()
        assert filt._presence_num_ctx == 0
        payloads = _run_and_capture_payloads(filt, repo)

        # With only 6 retries (2 per tier), the highest tier reached is
        # tier 2 (16384) — well under the flat 32768 ceiling, so nothing
        # is capped yet; this just confirms the ceiling isn't 0.
        assert max(p["max_tokens"] for p in payloads[1:]) == 16384

    def test_recovers_when_a_later_attempt_produces_parseable_json(self, repo):
        """A later retry — wider tokens or a different temperature — can
        be the actual reason a stuck candidate finally resolves, rather
        than failing closed after exhausting every retry."""
        filt = _make_filter()
        real_build = llm_stream_mod.build_chat_request
        captured: list[dict] = []

        def _capture(**kwargs):
            result = real_build(**kwargs)
            captured.append(result[2])
            return result

        good_verdict = '{"verdict": "confirmed", "evidence": "return raw", "reason": "x"}'
        side_effects = ["", "", good_verdict]  # initial + 2 retries, then success

        with patch("tools.llm_stream.build_chat_request", side_effect=_capture), \
             patch("tools.llm_stream.request_completion", side_effect=side_effects):
            accepted, rejected = filt.filter([_candidate()], repo)

        assert len(captured) == 3
        assert captured[2]["temperature"] == pytest.approx(0.1)  # retry 2: tier 0, 2nd temp
        assert len(accepted) == 1
        assert rejected == []
