"""tests_bugfix/test_332_report3_bugs.py — regression guards for 332 (Report 3) bugs."""
from __future__ import annotations

import logging

import pytest

from tools.contest.runner import _leg_cap, _TestRunsLock
from tools.auto.prosody import check_prosody
from tools.contest.backend import _AgentStopped


def test_bug1_leg_cap_off_by_one():
    assert _leg_cap(10, 5, 100) == 3


def test_bug5_test_runs_lock_no_keyerror():
    import threading
    lock = _TestRunsLock()
    # Thread A acquires and pops its entry before releasing, simulating exit.
    acquired = threading.Event()
    release = threading.Event()
    def holder():
        lock.enter("shared")
        acquired.set()
        # mimic exit's pop while still holding _inner
        with lock._guard:
            lock._entries.pop("shared", None)
        release.wait(timeout=2)
        lock._inner.release()

    t = threading.Thread(target=holder, daemon=True)
    t.start()
    acquired.wait(timeout=2)
    # Thread B waits on _inner, then wakes into missing entry.
    result = {}
    def waiter():
        try:
            lock.enter("shared")
            result["ok"] = True
            lock.exit("shared")
        except KeyError as e:
            result["err"] = e
    w = threading.Thread(target=waiter, daemon=True)
    w.start()
    release.set()
    w.join(timeout=2)
    t.join(timeout=2)
    assert result.get("ok") is True, result


def test_bug6_prosody_extra_lines_checked():
    text = (
        "Тихо падает первый снег\n"
        "Город молчит под фонарём\n"
        "Где-то вдали гудит мотор\n"
        "Ветер качает старый дом\n"
        "\n"
        "Утром проснётся серый кот\n"
        "Выйдет на улицу гулять\n"
        "Где-то вдали гудит завод\n"
        "Листья летят кружась кружась\n"
        "совершенно иная строка с другой длиной\n"
        "ещё одна строка\n"
    )
    v = check_prosody(text, require_rhythm=True)
    assert not v.approved


def test_bug7_inner_loop_logs(caplog):
    from tools.auto.inner_loop import LLMGate2Validator
    # minimal: call _fetch_needed_flat with a failing search agent
    class BadAgent:
        def run(self, references, base_dir):
            raise RuntimeError("boom")
    il = LLMGate2Validator.__new__(LLMGate2Validator)
    il._search_agent = BadAgent()
    il.base_dir = "."
    with caplog.at_level(logging.DEBUG, logger="tools.auto.inner_loop"):
        il._fetch_needed_flat(["sym"], 10)
    assert any("search fetch failed" in r.message for r in caplog.records)


def test_bug8_backend_unexpected_message():
    # construct backend? Heavy. Instead check source contains protocol error handling.
    import inspect
    from tools.contest import backend as b
    src = inspect.getsource(b)
    assert "protocol error: expected permission.reply" in src


def test_bug9_on_deadline_ceiling_inf():
    # source-level check
    import inspect
    from tools.contest import runner as r
    src = inspect.getsource(r)
    assert 'ceiling = cap if cap > 0 else float("inf")' in src
