"""tests/test_cr33_task_wide_deadline.py — the wall-clock budget is task-wide,
shared across outer-loop rounds (AUTO-CR-33).

Bug: InnerLoop.run_task reset its start time every round, so a 30-min
max_task_seconds actually allowed max_rounds × 30 min (~5 h observed). The outer
loop must (a) stop before starting a round once the budget is gone, and (b) hand
the shared deadline to the inner loop.
"""
from __future__ import annotations

import tools.auto.outer_loop as ol_mod
from tools.auto.outer_loop import OuterLoop


class _Res:
    def __init__(self, passed): self.passed = passed; self.attempts_used = 1
    last_feedback = ""; records = []; task_id = "T"


class _Inner:
    def __init__(self, mts):
        self.max_task_seconds = mts
        self.calls = 0
        self.seen_deadline = "UNSET"
    def run_task(self, task, base_dir, *, prior_feedback=None,
                 prior_implementations=None, deadline=None):
        self.calls += 1
        self.seen_deadline = deadline
        return _Res(passed=False)        # never passes → would loop rounds


class _State:
    def __getattr__(self, _name):
        return lambda *a, **k: None


def _outer(inner):
    o = object.__new__(OuterLoop)
    o.max_rounds = 10
    o.inner_loop = inner
    o.state = _State()
    o._existing_rounds = lambda t: 0
    o._feedback_paths = lambda t: []
    o._read_round_feedback = lambda t: []
    o._build_impl_history = lambda *a, **k: []
    o.task_rewriter = None
    return o


def test_outer_stops_when_budget_exhausted(monkeypatch):
    # monotonic: 1st call (deadline calc) = 1000; every later call = 9999 (past 1000+30)
    seq = {"n": 0}
    def fake_mono():
        seq["n"] += 1
        return 1000.0 if seq["n"] == 1 else 9999.0
    monkeypatch.setattr(ol_mod.time, "monotonic", fake_mono)

    inner = _Inner(mts=30)
    res = _outer(inner).run_task({"id": "T"}, "/tmp")
    assert inner.calls == 0          # never started a round past the deadline
    assert res.passed is False


def test_inner_receives_shared_deadline(monkeypatch):
    monkeypatch.setattr(ol_mod.time, "monotonic", lambda: 1000.0)  # never advances
    inner = _Inner(mts=1800)
    # passes on the first round so we can inspect the single call
    inner.run_task = (lambda task, base_dir, *, prior_feedback=None,
                      prior_implementations=None, deadline=None:
                      (setattr(inner, "seen_deadline", deadline) or _Res(passed=True)))
    _outer(inner).run_task({"id": "T"}, "/tmp")
    assert inner.seen_deadline == 1000.0 + 1800   # shared task-wide deadline


def test_no_budget_means_no_deadline(monkeypatch):
    monkeypatch.setattr(ol_mod.time, "monotonic", lambda: 5.0)
    inner = _Inner(mts=0)            # guard disabled
    inner.run_task = (lambda task, base_dir, *, prior_feedback=None,
                      prior_implementations=None, deadline=None:
                      (setattr(inner, "seen_deadline", deadline) or _Res(passed=True)))
    _outer(inner).run_task({"id": "T"}, "/tmp")
    assert inner.seen_deadline is None
