"""Every [progress] / [plan] line the voting pool writes to stderr is whole, never two run together (PROGRESS-LINE)."""

from __future__ import annotations

import concurrent.futures
import re
import sys
import threading
import time
from pathlib import Path

from tools.auto.llm_profile import LlmSettings

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import claim_diff  # noqa: E402
import claim_vote as cv  # noqa: E402

THREADS = 8


class _SlowStderr:
    """A stream that yields after every write, so two threads that write a line in two
    parts (`print`'s text, then its newline) interleave every time."""

    def __init__(self):
        self.parts = []

    def write(self, text):
        self.parts.append(text)
        time.sleep(0.002)
        return len(text)

    def flush(self):
        pass

    def lines(self):
        return "".join(self.parts).splitlines()


class _Pacer:
    def wait(self, host, deadline=None):
        return threading.Semaphore()


def test_the_request_lines_of_one_voter_are_whole(monkeypatch):
    err = _SlowStderr()
    monkeypatch.setattr(sys, "stderr", err)
    monkeypatch.setattr(cv, "voter_settings", lambda ref, parser: LlmSettings(
        base_url="http://x.invalid/v1", api_key="k", model=ref, temperature=0.3, max_tokens=100))
    monkeypatch.setattr(cv, "request_completion", lambda *a, **k: "[]")
    barrier = threading.Barrier(THREADS)

    def ask_with_packs(items, run, seed, complete, **_k):
        def one(_):
            barrier.wait()
            for _ in range(3):
                complete("prompt")
        with concurrent.futures.ThreadPoolExecutor(THREADS) as pool:
            list(pool.map(one, range(THREADS)))
        return {"votes": {}, "accepted": {}, "rejected": [], "errors": [], "raw_head": ""}
    monkeypatch.setattr(cv.cc_judge, "ask_with_packs", ask_with_packs)

    row = cv.ask_v2("x/m", [], 0, 1, None, 30, pacer=_Pacer())
    assert row["requests"] == THREADS * 3
    lines = err.lines()
    assert len(lines) == THREADS * 3, lines                       # was fewer: two lines on one
    assert all(re.fullmatch(r"\[progress\] x/m run0 request \d+ \d+s", ln) for ln in lines), lines


def test_the_job_lines_of_the_pool_are_whole(monkeypatch):
    err = _SlowStderr()
    monkeypatch.setattr(sys, "stderr", err)
    barrier = threading.Barrier(THREADS)

    def ask(ref, items, run):
        barrier.wait()
        return {"votes": {}, "rejected": []}

    voters = [f"p/v{i}" for i in range(THREADS)]
    with concurrent.futures.ThreadPoolExecutor(THREADS) as pool:
        rows = claim_diff.make_vote(voters, 2, pool, ask)([], "head")
    assert len(rows) == THREADS * 2
    lines = err.lines()
    assert len(lines) == THREADS * 2, lines
    assert all(re.fullmatch(r"\[progress\] head p/v\d run\d done: votes=0/0 rejected=0 job \d+/16 \d+s", ln)
               for ln in lines), lines
