"""tests_bugfix/test_bugfix_run2_budget_pauses_when_stopped.py — RUN-2.

The per-task wall-clock budget (AUTO-CR-33) used to survive a restart by
persisting the task's first start time in
``.agent/tasks/<id>/deadline_started_at.txt`` and deducting

    elapsed   = time.time() - started_at
    remaining = max(max_task_seconds - elapsed, 0)

on every entry to ``OuterLoop.run_task``. That is *calendar* time, not *run*
time, so a run stopped in the evening woke up with every already-started task
over budget before a single LLM call:

    05:56:10 task AUTO-T14 wall-clock budget (1800s = 30.0 min) exhausted
             across rounds — stopping before round 10.      … BLOCKED
    05:57:33 task AUTO-T27 wall-clock budget (1800s = 30.0 min) exhausted
             across rounds — stopping before round 4.       … BLOCKED

AUTO-T27 had used 3 of 10 rounds. It was not exhausted; the clock was.

The audit fix's intent is right — a restart must not hand a runaway task a
fresh 30 minutes — so the budget must still accumulate across restarts. What
must change is the quantity persisted: seconds the task was actually worked,
summed over sessions, instead of "seconds since the task was first seen".

Fix under test
--------------
The file now holds a ledger —

    {"consumed_s": 1234.5, "session_started_at": 1757820964.1}

``consumed_s`` is summed over sessions; ``session_started_at`` is present only
while a session is active and is folded in, capped at the budget, if the
previous session died before closing it. ``run_task`` opens the ledger at entry
and closes it from a ``finally``, so every exit path folds the session's time
in exactly once.

Nothing here re-implements OuterLoop: every scenario drives the real
``run_task`` against a real ``StateStore`` on disk with a controllable clock.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

import tools.auto.outer_loop as ol_mod
from tools.auto.outer_loop import OuterLoop, parse_budget_file, render_budget_file
from tools.auto.state import StateStore, make_task


BUDGET_FILE = "deadline_started_at.txt"
MBS = 1800          # agents.ini [auto] max_task_seconds
TASK_ID = "AUTO-T27"
NIGHT_SECONDS = 8 * 3600.0


# ── controllable clocks ──────────────────────────────────────────────────────


class FakeClock:
    """Wall and monotonic clocks the harness can advance independently.

    ``advance`` is a session that is genuinely working (both move);
    ``advance_wall`` is a stop (only ``time.time()`` moves). Splitting them is
    how these tests express the bug at all: the pre-RUN-2 code deducted
    ``time.time() - started_at``, so a night stopped charged a whole night of
    budget, and there is no monotonic-only way to reproduce it.
    """

    def __init__(self, wall: float = 1_000_000.0, mono: float = 0.0):
        self.wall = wall
        self.mono = mono

    def advance(self, seconds: float) -> None:
        self.wall += seconds
        self.mono += seconds

    def advance_wall(self, seconds: float) -> None:
        self.wall += seconds

    def time(self) -> float:
        return self.wall

    def monotonic(self) -> float:
        return self.mono


def _patch_clocks(monkeypatch, clock: FakeClock) -> None:
    monkeypatch.setattr(ol_mod.time, "time", clock.time)
    monkeypatch.setattr(ol_mod.time, "monotonic", clock.monotonic)


# ── harness ──────────────────────────────────────────────────────────────────


def _store(tmp_path: Path) -> StateStore:
    state = StateStore(tmp_path / ".agent")
    state.initialise("goal", tmp_path)
    state.upsert_task(make_task(
        id=TASK_ID, title="t", instruction="i",
        target_files=["a.py"], acceptance_check="true",
    ))
    return state


def _write_closed_ledger(state: StateStore, clock: FakeClock,
                         consumed_s: float) -> float:
    """Write a closed ledger and return the wall clock it was written at."""
    state.write_task_file(TASK_ID, BUDGET_FILE, render_budget_file(consumed_s))
    return clock.time()


def _read_ledger(state: StateStore) -> dict:
    return json.loads(state.read_task_file(TASK_ID, BUDGET_FILE))


class _Res:
    def __init__(self, passed: bool):
        self.passed = passed
        self.attempts_used = 1
        self.context_satisfied = True
    last_feedback = ""


class FakeInner:
    """Runs one round per call, sleeping *sleep_s* and passing on demand.

    ``sleep_s`` advances BOTH clocks, which is how a session's real work time
    is expressed without the test actually waiting for it. The budget handed in
    by the deadline is snapshotted at call time — measuring it afterwards would
    read the clock that this very round just consumed.
    """

    max_task_seconds = MBS

    def __init__(self, clock: FakeClock, sleep_s: float = 0.0, passed: bool = False):
        self.clock = clock
        self.sleep_s = sleep_s
        self.passed = passed
        self.calls = 0
        self.deadlines: list = []
        self.remaining: list = []

    def run_task(self, task, base_dir, *, prior_feedback=None,
                 prior_implementations=None, deadline=None):
        self.calls += 1
        self.deadlines.append(deadline)
        self.remaining.append(deadline - self.clock.monotonic() if deadline else None)
        self.clock.advance(self.sleep_s)
        return _Res(passed=self.passed)


class InterruptedInner(FakeInner):
    """Ctrl-C in the middle of a round.

    A real stop, not a failed round: the session consumes its time and dies
    before the round's feedback file is written, so a resume picks the same
    round back up. This is the case that leaves the ledger's marker behind
    unless run_task's ``finally`` closes it.
    """

    def run_task(self, task, base_dir, *, prior_feedback=None,
                 prior_implementations=None, deadline=None):
        self.calls += 1
        self.deadlines.append(deadline)
        self.remaining.append(deadline - self.clock.monotonic() if deadline else None)
        self.clock.advance(self.sleep_s)
        raise KeyboardInterrupt


def _outer(state: StateStore, inner: FakeInner, max_rounds: int = 1) -> OuterLoop:
    return OuterLoop(inner, state, max_rounds=max_rounds)


# ── the ticket's acceptance cases ────────────────────────────────────────────


class TestBudgetDoesNotTickWhileStopped:

    def test_worked_600s_stopped_8h_resumes_with_1200s(self, tmp_path, monkeypatch):
        """A task worked for 600 s, "stopped" for 8 h, resumed → it gets
        1800 - 600 = 1200 s, not 0, and the round actually proceeds."""
        clock = FakeClock()
        _patch_clocks(monkeypatch, clock)

        state = _store(tmp_path)
        # Session 1: one genuine 600 s of work, then the operator stops the run.
        inner1 = InterruptedInner(clock, sleep_s=600.0)
        with pytest.raises(KeyboardInterrupt):
            _outer(state, inner1).run_task(state.get_task(TASK_ID), tmp_path)
        assert inner1.calls == 1
        ledger = _read_ledger(state)
        assert ledger["consumed_s"] == pytest.approx(600.0, abs=0.01)
        assert "session_started_at" not in ledger, (
            "a clean stop must close the ledger — leaving it open makes the "
            "next resume fold a whole night into consumed_s"
        )

        # The overnight stop: wall clock only. monotonic has no fixed epoch and
        # is not part of the ledger, so the resumed session must not see it.
        clock.advance_wall(NIGHT_SECONDS)

        # Session 2: a fresh process, same task, same ledger on disk.
        inner2 = FakeInner(clock, passed=True)
        res2 = _outer(state, inner2).run_task(state.get_task(TASK_ID), tmp_path)

        assert res2.passed is True, (
            "the budget ticked while the run was stopped: the resumed task was "
            "declared exhausted before it did anything"
        )
        assert inner2.calls == 1, "the resumed task must actually run a round"
        assert inner2.remaining[0] == pytest.approx(MBS - 600.0, abs=1.0), (
            f"expected {MBS - 600.0:.0f} s left after a night stopped, got "
            f"{inner2.remaining[0]:.1f}"
        )

    def test_unclosed_session_is_capped_at_one_budget(self, tmp_path, monkeypatch):
        """A session that died with the marker still set, 8 h ago, consumed at
        most ``max_task_seconds`` — never the 8 h it sat un-closed."""
        clock = FakeClock()
        _patch_clocks(monkeypatch, clock)

        state = _store(tmp_path)
        before = _write_closed_ledger(state, clock, 200.0)
        # SIGKILL / power loss: the marker was never cleared, and the session
        # has been "dead" for 8 hours.
        clock.advance(NIGHT_SECONDS)
        state.write_task_file(TASK_ID, BUDGET_FILE, render_budget_file(
            200.0, session_started_at=before,
        ))

        inner = FakeInner(clock, passed=True)
        res = _outer(state, inner).run_task(state.get_task(TASK_ID), tmp_path)

        assert res.passed is False and res.exhausted is True
        assert inner.calls == 0, "a budget that was really spent must not run"
        ledger = _read_ledger(state)
        consumed = ledger["consumed_s"]
        assert consumed == pytest.approx(200.0 + MBS, abs=1.0), (
            "a crash mid-round must cost at most one budget — the open session "
            f"was folded in at {consumed - 200.0:.0f} s instead of <= {MBS}"
        )
        assert consumed < 200.0 + NIGHT_SECONDS, "the night was charged as consumed time"
        assert "session_started_at" not in ledger

    def test_unclosed_session_below_the_cap_is_charged_in_full(
            self, tmp_path, monkeypatch):
        """A short unclosed session is charged at full rate — the cap only
        applies to the runaway case, and a task with budget left still runs."""
        clock = FakeClock()
        _patch_clocks(monkeypatch, clock)

        state = _store(tmp_path)
        before = _write_closed_ledger(state, clock, 100.0)
        clock.advance(800.0)
        state.write_task_file(TASK_ID, BUDGET_FILE, render_budget_file(
            100.0, session_started_at=before,
        ))

        inner = FakeInner(clock, passed=True)
        res = _outer(state, inner).run_task(state.get_task(TASK_ID), tmp_path)

        assert res.passed is True
        assert inner.calls == 1
        assert inner.remaining[0] == pytest.approx(MBS - 900.0, abs=1.0)
        assert _read_ledger(state)["consumed_s"] == pytest.approx(900.0, abs=1.0)

    def test_budget_still_accumulates_across_restarts(self, tmp_path, monkeypatch):
        """AUTO-CR-33 must still hold: the budget accumulates across restarts,
        so a task that burned 1600 s of a 1800 s budget gets its 200 s left,
        one round, then the existing ``exhausted across rounds`` WARNING for the
        session's second round.

        Session 1 burns four 400 s rounds (1600 s) and exhausts its rounds;
        session 2 keeps the 200 s, runs one round, then refuses the next.
        """
        clock = FakeClock()
        _patch_clocks(monkeypatch, clock)

        state = _store(tmp_path)
        inner1 = FakeInner(clock, sleep_s=400.0, passed=False)
        res1 = _outer(state, inner1, max_rounds=4).run_task(
            state.get_task(TASK_ID), tmp_path)
        assert res1.exhausted is True
        assert inner1.calls == 4
        assert _read_ledger(state)["consumed_s"] == pytest.approx(1600.0, abs=1.0)

        # A stop between the sessions — harmless now, and must stay harmless if
        # that stop is a night: nothing may be charged for it.
        clock.advance(NIGHT_SECONDS)

        inner2 = FakeInner(clock, sleep_s=400.0, passed=False)
        res2 = _outer(state, inner2, max_rounds=10).run_task(
            state.get_task(TASK_ID), tmp_path)

        assert res2.exhausted is True
        assert inner2.calls == 1, (
            "only the 200 s left over from session 1 may run — the budget "
            "stopped accumulating across restarts (the AUTO-CR-33 regression)"
        )
        assert inner2.remaining[0] == pytest.approx(200.0, abs=1.0)
        assert _read_ledger(state)["consumed_s"] == pytest.approx(2000.0, abs=1.0)

    def test_legacy_float_file_migrates_to_zero_consumed(self, tmp_path, monkeypatch,
                                                          caplog):
        """A float-only legacy file is unknown, not exhausted: one WARNING, a
        full budget, and the file rewritten in the current format."""
        clock = FakeClock()
        _patch_clocks(monkeypatch, clock)
        # A timestamp in the past, exactly like the files the old code wrote.
        clock.advance(NIGHT_SECONDS)

        state = _store(tmp_path)
        state.write_task_file(TASK_ID, BUDGET_FILE, repr(clock.wall - NIGHT_SECONDS))

        caplog.set_level(logging.WARNING, logger="tools.auto.outer_loop")
        inner = FakeInner(clock, passed=True)
        res = _outer(state, inner).run_task(state.get_task(TASK_ID), tmp_path)

        assert res.passed is True, (
            "the legacy file was read as an exhausted budget — its timestamp "
            "pre-dates the whole run, so the old code blocked a task that merely "
            "existed earlier"
        )
        assert inner.calls == 1
        assert inner.remaining[0] == pytest.approx(MBS, abs=1.0)
        legacy_warnings = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and "legacy" in r.getMessage()
        ]
        assert len(legacy_warnings) == 1, caplog.text
        ledger = _read_ledger(state)
        assert "consumed_s" in ledger and ledger["consumed_s"] >= 0.0
        assert "session_started_at" not in ledger

    def test_fresh_task_gets_the_full_budget(self, tmp_path, monkeypatch):
        clock = FakeClock()
        _patch_clocks(monkeypatch, clock)

        state = _store(tmp_path)
        assert state.read_task_file(TASK_ID, BUDGET_FILE) is None
        inner = FakeInner(clock, passed=True)
        res = _outer(state, inner).run_task(state.get_task(TASK_ID), tmp_path)

        assert res.passed is True
        assert inner.remaining[0] == pytest.approx(MBS, abs=1.0)
        ledger = _read_ledger(state)
        assert ledger["consumed_s"] == pytest.approx(0.0, abs=1.0)
        assert "session_started_at" not in ledger, (
            "a finished session must not leave an open session behind"
        )

    def test_no_budget_configured_means_no_ledger(self, tmp_path, monkeypatch):
        """max_task_seconds = 0 disables the guard: no file is created and the
        deadline stays None (unchanged behaviour)."""
        clock = FakeClock()
        _patch_clocks(monkeypatch, clock)

        class NoBudget(FakeInner):
            max_task_seconds = 0

        state = _store(tmp_path)
        inner = NoBudget(clock, passed=True)
        res = _outer(state, inner).run_task(state.get_task(TASK_ID), tmp_path)

        assert res.passed is True
        assert inner.deadlines[0] is None
        assert state.read_task_file(TASK_ID, BUDGET_FILE) is None

    def test_budget_guard_absent_from_inner_loop(self, tmp_path, monkeypatch):
        """A fake/old inner loop with no max_task_seconds attribute at all →
        guard disabled, no crash, no ledger."""
        clock = FakeClock()
        _patch_clocks(monkeypatch, clock)

        class NoAttribute:
            def run_task(self, task, base_dir, *, prior_feedback=None,
                         prior_implementations=None, deadline=None):
                return _Res(passed=True)

        state = _store(tmp_path)
        res = _outer(state, NoAttribute()).run_task(state.get_task(TASK_ID), tmp_path)

        assert res.passed is True
        assert state.read_task_file(TASK_ID, BUDGET_FILE) is None

    def test_resume_with_consumed_time_logs_once(self, tmp_path, monkeypatch, caplog):
        """A resume that starts with consumed time must say so — one INFO line,
        so an operator can tell "picking up where it left off" from
        "the budget vanished". A fresh task logs nothing."""
        clock = FakeClock()
        _patch_clocks(monkeypatch, clock)

        state = _store(tmp_path)
        _write_closed_ledger(state, clock, 600.0)

        caplog.set_level(logging.INFO, logger="tools.auto.outer_loop")
        res = _outer(state, FakeInner(clock, passed=True)).run_task(
            state.get_task(TASK_ID), tmp_path)

        assert res.passed is True
        resumes = [
            r for r in caplog.records
            if r.levelno == logging.INFO and "resumes with" in r.getMessage()
        ]
        assert len(resumes) == 1, caplog.text
        assert "600 s" in resumes[0].getMessage() and "1800 s" in resumes[0].getMessage()

        caplog.clear()
        _write_closed_ledger(state, clock, 0.0)
        res = _outer(state, FakeInner(clock, passed=True)).run_task(
            state.get_task(TASK_ID), tmp_path)
        assert res.passed is True
        assert not [
            r for r in caplog.records if "resumes with" in r.getMessage()
        ]


# ── ledger parsing (the format contract) ─────────────────────────────────────


class TestBudgetLedgerParsing:

    def test_missing_and_empty_are_fresh(self):
        assert parse_budget_file(None, 5.0, MBS) == (0.0, False)
        assert parse_budget_file("   ", 5.0, MBS) == (0.0, False)

    def test_closed_ledger_is_read_as_is(self):
        assert parse_budget_file(render_budget_file(600.0), 5.0, MBS) == (600.0, False)

    def test_open_session_is_folded_in_at_full_rate(self):
        # 300 s of real work in the open session, nowhere near the cap.
        consumed, legacy = parse_budget_file(
            render_budget_file(100.0, session_started_at=1000.0),
            now=1300.0, max_seconds=MBS,
        )
        assert legacy is False
        assert consumed == pytest.approx(400.0)

    def test_open_session_is_capped_at_the_budget(self):
        assert parse_budget_file(
            render_budget_file(100.0, session_started_at=1000.0),
            now=1000.0 + 24 * 3600.0, max_seconds=MBS,
        ) == (100.0 + MBS, False)

    def test_open_session_negative_gap_is_not_credited(self):
        # A clock that went backwards must not hand a task more budget.
        assert parse_budget_file(
            render_budget_file(100.0, session_started_at=9000.0),
            now=1000.0, max_seconds=MBS,
        ) == (100.0, False)

    def test_legacy_float_is_flagged_and_zeroed(self):
        assert parse_budget_file("1700000000.0", 1757820964.0, MBS) == (0.0, True)
        assert parse_budget_file("1700000000", 1757820964.0, MBS) == (0.0, True)

    def test_garbage_is_zero_not_a_crash(self):
        for raw in ("not a number", "", '{"consumed_s": "six"}', "[1,2]",
                    "null", '"1700000000.0"', "{}"):
            assert parse_budget_file(raw, 5.0, MBS) == (0.0, False), raw

    def test_negative_and_boolean_fields_are_clamped(self):
        assert parse_budget_file('{"consumed_s": -500}', 5.0, MBS) == (0.0, False)
        assert parse_budget_file('{"consumed_s": true}', 5.0, MBS) == (0.0, False)
        assert parse_budget_file(
            '{"consumed_s": 10, "session_started_at": true}', 5.0, MBS
        ) == (10.0, False)

    def test_nan_and_infinity_are_unknown_not_poison(self):
        """json.loads accepts NaN/Infinity; a NaN consumed_s would never read as
        exhausted and an Infinity one always would — both degrade to unknown."""
        assert parse_budget_file('{"consumed_s": NaN}', 5.0, MBS) == (0.0, False)
        assert parse_budget_file('{"consumed_s": Infinity}', 5.0, MBS) == (0.0, False)
        assert parse_budget_file(
            '{"consumed_s": 10, "session_started_at": NaN}', 5.0, MBS
        ) == (10.0, False)

    def test_render_round_trips(self):
        assert parse_budget_file(render_budget_file(1234.5), 0.0, MBS) == (1234.5, False)
        assert json.loads(render_budget_file(0.0)) == {"consumed_s": 0.0}
        assert "session_started_at" not in render_budget_file(1.0)
