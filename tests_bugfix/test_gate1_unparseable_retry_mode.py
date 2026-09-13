"""tests_bugfix/test_gate1_unparseable_retry_mode.py — GATE1-LEARN-2.

Field report (log-test.txt): presence check on a thinking model,
[gate1_llm] max_tokens = 32768, num_ctx = 512000, provider cap 65536.
33 candidates, 63 calls, 3.5 h. When the model answered it took 6-60 s;
when it did not it returned an EMPTY reply after burning the whole
budget (~300 s at 32k, ~650 s at 64k). The ladder then:
  * repeated the initial call's exact settings (32768/0.0 -> 32768/0.0),
  * doubled a budget that was not the problem (empty at 64k too),
  * asked for 131072 twice -> HTTP 400 from the provider.

Fix — three opt-in [gate1] knobs, every default = the old behaviour:
  unparseable_retry_mode = strict|fast   (default strict)
  unparseable_max_tokens_cap = N         (default 0 = off)
  unparseable_max_retries = N            (default 6)
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

GOOD = '{"verdict": "confirmed", "evidence": "return raw", "reason": "x"}'
GARBLED = '{"verdict": "confir'  # non-empty, truncated → still climbs


@pytest.fixture(autouse=True)
def _reset_caches():
    llm_stream_mod._REASONING_UNSUPPORTED_KEYS.clear()
    yield
    llm_stream_mod._REASONING_UNSUPPORTED_KEYS.clear()


def _make_filter(max_tokens="32768", num_ctx="512000", **gate1_extra) -> Gate1Filter:
    cfg = configparser.ConfigParser()
    cfg.read_dict({
        "api":       {"active": "local", "verify_ssl": "false"},
        "api_local": {
            "base_url": "http://localhost:1337/v1", "api_key": "test",
            "model": "test-model", "api_format": "openai", "num_ctx": num_ctx,
        },
        "gate1": {"temperature": "0.0", "max_tokens": max_tokens,
                  "skip_llm": "false", **gate1_extra},
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


def _candidate(n: int = 0) -> CandidateTask:
    return CandidateTask(
        title=f"Add input validation {n}", instruction="validate that raw is a dict",
        target_files=["tools/utils.py"], acceptance_check="true",
        cited_location=CitedLocation(file="tools/utils.py", symbol="parse_config"),
    )


def _run(filt: Gate1Filter, repo: Path, replies: list[str], n_candidates: int = 1):
    real_build = llm_stream_mod.build_chat_request
    captured: list[dict] = []

    def _capture(**kwargs):
        result = real_build(**kwargs)
        captured.append(result[2])
        return result

    with patch("tools.llm_stream.build_chat_request", side_effect=_capture), \
         patch("tools.llm_stream.request_completion", side_effect=replies):
        accepted, rejected = filt.filter(
            [_candidate(i) for i in range(n_candidates)], repo)
    return captured, accepted, rejected


def _grid(payloads):
    return [(p["max_tokens"], round(p["temperature"], 2)) for p in payloads]


class TestStrictIsTheOldLadder:

    def test_default_mode_reproduces_the_field_log(self, repo):
        # log-test.txt candidate #7: 32768/0.0 → 32768/0.0, 32768/0.1,
        # 65536/0.0, 65536/0.1, 131072/0.0 (HTTP 400 in the field), 131072/0.1
        filt = _make_filter()
        assert filt._unparseable_retry_mode == "strict"
        payloads, _, rejected = _run(filt, repo, [""] * 7)
        assert _grid(payloads) == [
            (32768, 0.0), (32768, 0.0), (32768, 0.1), (65536, 0.0),
            (65536, 0.1), (131072, 0.0), (131072, 0.1),
        ]
        assert len(rejected) == 1

    def test_cap_alone_keeps_the_ladder_but_never_exceeds_the_provider(self, repo):
        filt = _make_filter(unparseable_max_tokens_cap="65536")
        payloads, _, _ = _run(filt, repo, [""] * 7)
        assert max(p["max_tokens"] for p in payloads) == 65536
        assert len(payloads) == 7  # strict: every step still made

    def test_max_retries_is_configurable(self, repo):
        filt = _make_filter(unparseable_max_retries="2")
        payloads, _, rejected = _run(filt, repo, [""] * 3)
        assert len(payloads) == 3  # initial + 2
        assert len(rejected) == 1


class TestFastMode:

    def test_never_repeats_the_initial_call_and_empty_pins_the_tier(self, repo):
        # candidate #7 in fast mode: initial 32768/0.0 empty → only the
        # other temperature at the SAME tier, then fail-closed: 2 calls, not 6.
        filt = _make_filter(unparseable_retry_mode="fast")
        payloads, _, rejected = _run(filt, repo, [""] * 7)
        assert _grid(payloads) == [(32768, 0.0), (32768, 0.1)]
        assert len(rejected) == 1

    def test_second_temperature_answers(self, repo):
        # candidates #25-27: succeeded at 32768/0.1 on the 3rd call — now the 2nd.
        filt = _make_filter(unparseable_retry_mode="fast")
        payloads, accepted, _ = _run(filt, repo, ["", GOOD])
        assert _grid(payloads) == [(32768, 0.0), (32768, 0.1)]
        assert len(accepted) == 1

    def test_garbled_non_empty_reply_still_climbs(self, repo):
        # truncated JSON IS a budget problem — the ladder must still double.
        filt = _make_filter(unparseable_retry_mode="fast",
                            unparseable_max_tokens_cap="65536")
        payloads, accepted, _ = _run(filt, repo, [GARBLED, GARBLED, GOOD])
        assert _grid(payloads) == [(32768, 0.0), (32768, 0.1), (65536, 0.0)]
        assert len(accepted) == 1

    def test_cap_collapses_duplicate_tiers(self, repo):
        # after the cap, 65536 and 131072 are the same call — made once per temperature.
        filt = _make_filter(unparseable_retry_mode="fast",
                            unparseable_max_tokens_cap="65536")
        payloads, _, _ = _run(filt, repo, [GARBLED] * 7)
        assert _grid(payloads) == [
            (32768, 0.0), (32768, 0.1), (65536, 0.0), (65536, 0.1)]

    def test_empty_after_a_climb_pins_at_that_tier(self, repo):
        filt = _make_filter(unparseable_retry_mode="fast",
                            unparseable_max_tokens_cap="65536")
        # garbled → climb to 65536; empty there → do not go higher, try 0.1, stop
        payloads, _, _ = _run(filt, repo, [GARBLED, GARBLED, "", "", ""])
        assert _grid(payloads) == [
            (32768, 0.0), (32768, 0.1), (65536, 0.0), (65536, 0.1)]

    def test_small_configured_budget_still_climbs_in_fast_mode(self, repo):
        # 512 configured, empty reply: a 512 budget really can be the
        # problem (a <think> block truncated before any answer strips to
        # ""), so the empty-pin is only honoured at/above the ladder floor.
        filt = _make_filter(max_tokens="512", num_ctx="",
                            unparseable_retry_mode="fast")
        payloads, accepted, _ = _run(filt, repo, ["", GOOD])
        assert payloads[1]["max_tokens"] == 4096
        assert len(accepted) == 1

    def test_learned_start_still_works_in_fast_mode(self, repo):
        filt = _make_filter(max_tokens="512", num_ctx="",
                            unparseable_retry_mode="fast")
        payloads, accepted, _ = _run(
            filt, repo, [GARBLED, GARBLED, GOOD, GOOD], n_candidates=2)
        assert [p["max_tokens"] for p in payloads] == [512, 4096, 4096, 4096]
        assert len(accepted) == 2


class TestConfig:

    def test_malformed_values_fall_back_to_defaults(self, repo):
        filt = _make_filter(unparseable_retry_mode="turbo",
                            unparseable_max_tokens_cap="lots",
                            unparseable_max_retries="many")
        assert filt._unparseable_retry_mode == "strict"
        assert filt._unparseable_max_tokens_cap == 0
        assert filt._unparseable_max_retries == 6
