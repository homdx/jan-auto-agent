"""tests_bugfix/test_gate1_learned_start_budget.py — GATE1-LEARN-1.

Field report: ticket review (Gate-1 presence check) on a thinking model
climbed the unparseable ladder from the configured max_tokens for EVERY
candidate — 512 -> 4096 -> 4096 -> 8192 -> 8192 -> 16384 — five or six
calls and a 16k reply budget per ticket, rediscovering the same answer
each time. Nothing recorded the budget that finally produced a verdict.

Fix: the budget that produced a parseable verdict is recorded (bounded
window, per presence model) and the next candidate STARTS there — both
the first call and, if that still fails, the ladder's first tier.
The start is the median of the window, so one outlier does not pin
every later candidate to the ceiling. ``[gate1] unparseable_learn =
false`` restores the old behaviour.
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


@pytest.fixture(autouse=True)
def _reset_caches():
    llm_stream_mod._REASONING_UNSUPPORTED_KEYS.clear()
    yield
    llm_stream_mod._REASONING_UNSUPPORTED_KEYS.clear()


def _make_filter(**gate1_extra) -> Gate1Filter:
    cfg = configparser.ConfigParser()
    cfg.read_dict({
        "api":       {"active": "local", "verify_ssl": "false"},
        "api_local": {
            "base_url": "http://localhost:1337/v1", "api_key": "test",
            "model": "test-model", "api_format": "openai",
        },
        "gate1": {"temperature": "0.0", "max_tokens": "512",
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


def _run(filt: Gate1Filter, repo: Path, replies: list[str], n_candidates: int):
    """Feed *replies* to successive LLM calls; capture each payload's
    max_tokens/temperature. Returns (payloads, accepted, rejected)."""
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


class TestLearnedStart:

    def test_second_candidate_starts_where_the_first_succeeded(self, repo):
        filt = _make_filter()
        # candidate 0: 512 fails, 4096/0.0, 4096/0.1, 8192/0.0 fail, 8192/0.1 answers
        # candidate 1: must go out at 8192 and be answered in ONE call
        payloads, accepted, _ = _run(
            filt, repo, ["", "", "", "", GOOD, GOOD], n_candidates=2)
        assert [p["max_tokens"] for p in payloads] == [512, 4096, 4096, 8192, 8192, 8192]
        assert payloads[5]["temperature"] == pytest.approx(0.0)  # configured, not ladder
        assert len(accepted) == 2

    def test_first_candidate_is_unchanged(self, repo):
        filt = _make_filter()
        payloads, _, _ = _run(filt, repo, ["", GOOD], n_candidates=1)
        assert [p["max_tokens"] for p in payloads] == [512, 4096]

    def test_ladder_resumes_above_the_learned_budget(self, repo):
        filt = _make_filter()
        # cand 0 succeeds at 8192 (attempt 3); cand 1 fails at learned 8192,
        # then the ladder must NOT revisit 4096.
        payloads, _, _ = _run(
            filt, repo, ["", "", "", GOOD, "", "", "", GOOD], n_candidates=2)
        assert [p["max_tokens"] for p in payloads[4:]] == [8192, 8192, 8192, 16384]
        assert [p["temperature"] for p in payloads[5:]] == pytest.approx([0.0, 0.1, 0.0])

    def test_start_is_the_median_not_the_max(self, repo):
        filt = _make_filter()
        filt._record_parseable_budget(4096)
        filt._record_parseable_budget(16384)
        filt._record_parseable_budget(4096)
        assert filt._learned_max_tokens() == 4096
        filt._record_parseable_budget(8192)  # even window: (4096+8192)//2
        assert filt._learned_max_tokens() == 6144

    def test_window_is_bounded(self, repo):
        filt = _make_filter(unparseable_learn_window="2")
        for tok in (16384, 16384, 512, 512):
            filt._record_parseable_budget(tok)
        assert filt._unparseable_samples["test-model"] == [512, 512]

    def test_never_below_configured_max_tokens(self, repo):
        filt = _make_filter(max_tokens="1024")
        filt._record_parseable_budget(512)
        assert filt._learned_max_tokens() == 1024

    def test_samples_are_per_presence_model(self, repo):
        filt = _make_filter()
        filt._record_parseable_budget(8192)
        filt._presence_model = "other-model"
        assert filt._learned_max_tokens() is None

    def test_disabled_by_config(self, repo):
        filt = _make_filter(unparseable_learn="false")
        payloads, _, _ = _run(
            filt, repo, ["", "", "", "", GOOD, "", GOOD], n_candidates=2)
        # candidate 1 climbs from the configured floor again
        assert [p["max_tokens"] for p in payloads[5:]] == [512, 4096]

    def test_malformed_config_falls_back_to_defaults(self, repo):
        filt = _make_filter(unparseable_learn="maybe", unparseable_learn_window="lots")
        assert filt._unparseable_learn is True
        assert filt._unparseable_learn_window == 8
