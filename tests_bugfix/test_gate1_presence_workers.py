"""tests_bugfix/test_gate1_presence_workers.py — GATE1-PAR-1.

Field report (after GATE1-LEARN-2 landed): on a thinking model each
presence check that comes back EMPTY costs ~6 min of wall clock before the
single fast-mode re-ask (~30 s) answers. 403 candidates one after another
is a working day. The provider does not mind several in-flight requests;
it answers 429 + Retry-After when it does, which tools.llm_stream already
honours per call.

Fix — one opt-in [gate1] knob:
  presence_workers = N   (default 1 = the old sequential loop)

Stage B runs the LLM calls through a thread pool of N workers; the
outcomes are consumed in the original candidate order, so Stage C dedup,
plan order and "first wins" are unchanged; the learned-budget window is
locked between workers.
"""

from __future__ import annotations

import configparser
import json
import sys
import textwrap
import threading
import time
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
    llm_stream_mod._REASONING_UNSUPPORTED_KEYS.clear()
    yield
    llm_stream_mod._REASONING_UNSUPPORTED_KEYS.clear()


def _make_filter(**gate1_extra) -> Gate1Filter:
    cfg = configparser.ConfigParser()
    cfg.read_dict({
        "api":       {"active": "local", "verify_ssl": "false"},
        "api_local": {
            "base_url": "http://localhost:1337/v1", "api_key": "test",
            "model": "test-model", "api_format": "openai", "num_ctx": "32768",
        },
        "gate1": {"temperature": "0.0", "max_tokens": "4096",
                  "skip_llm": "false", "llm_call_retry_max": "0",
                  **gate1_extra},
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


def _candidate(n: int, new_file: bool = False) -> CandidateTask:
    return CandidateTask(
        title=f"Add input validation {n}", instruction=f"validate raw {n}",
        target_files=["tools/utils.py"], acceptance_check="true",
        cited_location=(
            CitedLocation(file="tools/new_mod.py", symbol="", new_file=True)
            if new_file else
            CitedLocation(file="tools/utils.py", symbol="parse_config")),
    )


def _verdict(confirmed: bool, n: int) -> str:
    return json.dumps({
        "verdict": "confirmed" if confirmed else "rejected",
        "evidence": "return raw" if confirmed else "",
        "reason": f"reason {n}",
    })


class _Provider:
    """Fake request_completion: answers per candidate index (parsed from
    the prompt's title), sleeping ``delay`` and recording concurrency."""

    def __init__(self, delay: float = 0.0, confirm=lambda n: n % 2 == 0):
        self.delay, self.confirm = delay, confirm
        self.lock = threading.Lock()
        self.in_flight = 0
        self.peak = 0
        self.order: list[int] = []

    def __call__(self, url, headers, payload, timeout, **kw):
        user = payload["messages"][-1]["content"]
        n = int(user.split("validate raw ")[1].split()[0].rstrip(":,.'\""))
        with self.lock:
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
            self.order.append(n)
        try:
            time.sleep(self.delay)
            return _verdict(self.confirm(n), n)
        finally:
            with self.lock:
                self.in_flight -= 1


def _run(filt, repo, provider, cands):
    with patch("tools.llm_stream.request_completion", side_effect=provider):
        return filt.filter(cands, repo)


class TestDefaultIsSequential:

    def test_default_one_worker(self):
        assert _make_filter()._presence_workers == 1

    def test_malformed_falls_back_to_one(self):
        assert _make_filter(presence_workers="many")._presence_workers == 1

    def test_zero_clamped_to_one(self):
        assert _make_filter(presence_workers="0")._presence_workers == 1

    def test_sequential_never_overlaps(self, repo):
        prov = _Provider(delay=0.02)
        accepted, rejected = _run(_make_filter(), repo, prov,
                                  [_candidate(i) for i in range(4)])
        assert prov.peak == 1
        assert prov.order == [0, 1, 2, 3]
        assert [c.title for c in accepted] == ["Add input validation 0",
                                               "Add input validation 2"]
        assert len(rejected) == 2


class TestParallel:

    def test_workers_overlap_and_order_is_preserved(self, repo):
        prov = _Provider(delay=0.15)
        cands = [_candidate(i) for i in range(6)]
        t0 = time.monotonic()
        accepted, rejected = _run(_make_filter(presence_workers="3"), repo, prov, cands)
        elapsed = time.monotonic() - t0
        assert prov.peak == 3
        assert elapsed < 0.15 * 6  # strictly faster than sequential
        # outcomes consumed in candidate order regardless of completion order
        assert [c.title for c in accepted] == [f"Add input validation {i}" for i in (0, 2, 4)]
        assert [r.candidate.title for r in rejected] == [f"Add input validation {i}" for i in (1, 3, 5)]
        assert all(r.stage == "presence" and r.reason == f"reason {i}"
                   for r, i in zip(rejected, (1, 3, 5)))

    def test_pool_is_capped_by_candidate_count(self, repo):
        prov = _Provider(delay=0.05)
        _run(_make_filter(presence_workers="10"), repo, prov,
             [_candidate(i) for i in range(2)])
        assert prov.peak == 2

    def test_new_file_candidates_skip_the_llm_and_keep_their_slot(self, repo):
        prov = _Provider(delay=0.02, confirm=lambda n: True)
        cands = [_candidate(0), _candidate(1, new_file=True), _candidate(2)]
        accepted, rejected = _run(_make_filter(presence_workers="4"), repo, prov, cands)
        assert sorted(prov.order) == [0, 2]
        assert [c.title for c in accepted] == [f"Add input validation {i}" for i in (0, 1, 2)]
        assert rejected == []

    def test_worker_exception_is_a_fail_closed_rejection_not_a_crash(self, repo):
        def boom(url, headers, payload, timeout, **kw):
            raise RuntimeError("provider exploded")
        with patch("tools.llm_stream.request_completion", side_effect=boom):
            accepted, rejected = _make_filter(presence_workers="3").filter(
                [_candidate(i) for i in range(3)], repo)
        assert accepted == []
        assert len(rejected) == 3
        assert all("LLM call failed" in r.reason for r in rejected)

    def test_learned_window_is_shared_and_bounded_under_workers(self, repo):
        prov = _Provider(delay=0.01, confirm=lambda n: True)
        filt = _make_filter(presence_workers="4", unparseable_learn_window="3")
        _run(filt, repo, prov, [_candidate(i) for i in range(12)])
        samples = filt._unparseable_samples["test-model"]
        assert len(samples) == 3
        assert filt._learned_max_tokens() == 4096
