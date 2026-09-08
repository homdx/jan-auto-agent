"""tests_bugfix/test_bugfix_upsert_reopen_done_fix.py — A1 follow-up.

A1 added a downgrade guard so a plain re-plan (plan_emitter.py:137 /
pipeline.py:452) cannot silently revert a DONE task back to TODO.  That part
is correct.

But the guard is *default-deny*: a DONE -> TODO transition only happens when
the caller passes ``allow_downgrade=True``.  BugFixLoop re-opens a synthetic
FIX task for a *new* regression by re-upserting the same deterministic
``fix_id`` at bug_fix_loop.py:660 — and that call must pass
``allow_downgrade`` or the new regression is never re-fixed.

Real sequence (driven here through the real BugFixLoop):
  1. first regression  -> fix task created (TODO) and run to success -> DONE
  2. second regression -> the SAME fix_id is built via _build_fix_task() and
     upserted again.  With A1's guard and NO allow_downgrade the task wrongly
     stays DONE (the new regression is silently never fixed).  With the fix it
     reopens to TODO so the fix actually runs.

The test FAILS on current code (task wrongly stays DONE) and PASSES once
bug_fix_loop.py passes allow_downgrade to the re-upsert.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.bug_fix_loop import BugFixLoop, BugFixResult


class _FakeOuter:
    """OuterLoop stand-in: records the fix task status *at entry* (i.e. what
    BugFixLoop's own upsert produced) then marks the task DONE to simulate a
    successful fix.  A second call records the status the loop reopened it to
    before doing any work."""

    def __init__(self, state):
        self._state = state
        self.status_at_entry = None

    def run_task(self, fix_task, base_dir):
        self.status_at_entry = self._state.get_task(fix_task["id"])["status"]
        self._state.set_task_status(fix_task["id"], "done", commit="deadbeef", impl_version=3)
        return "ok"


class _FakeTickets:
    """TicketStore stand-in: always "no existing ticket" so handle_regression
    proceeds straight to build + upsert the fix task."""

    def get(self, ticket_id):
        return None

    def create(self, ticket):
        return None

    def update(self, ticket_id, **fields):
        return True


class _FakeCos:
    def commit(self, *a, **k):
        return None


class _ExecResult:
    passed = False
    exit_code = 1
    stdout = "fail"
    stderr = ""
    traceback = ""


def _store(tmp_path):
    # Local import keeps the test importable even if StateStore moves.
    from tools.auto.state import StateStore
    agent = tmp_path / ".agent"
    agent.mkdir(parents=True, exist_ok=True)
    s = StateStore(agent)
    s.initialise("g", tmp_path)
    return s


def _loop(state):
    outer = _FakeOuter(state)
    loop = BugFixLoop(outer, _FakeCos(), _FakeTickets(), state, max_fix_attempts=5)
    return loop, outer


def _trigger(root_id: str) -> dict:
    return {
        "id": root_id,
        "title": f"task {root_id}",
        "target_files": ["a.py"],
        "acceptance_check": "python -c 'assert False'",
    }


class TestReopenDoneFixTask:
    def test_done_fix_task_reopens_for_new_regression(self, tmp_path):
        state = _store(tmp_path)
        loop, outer = _loop(state)
        root = "AUTO-T1"
        trig = _trigger(root)

        # 1. first regression -> fix runs to DONE.
        res1: BugFixResult = loop.handle_regression(trig, _ExecResult(), tmp_path)
        assert state.get_task(f"BUG-FIX-{root}")["status"] == "done"

        # 2. NEW regression on the same root id -> BugFixLoop re-upserts the
        #    same deterministic fix_id.  What matters is the status the loop's
        #    own upsert leaves the task in *before* any new work runs.
        loop.handle_regression(trig, _ExecResult(), tmp_path)
        assert outer.status_at_entry == "todo", (
            "BugFixLoop must reopen the DONE fix task to TODO for a new "
            f"regression, but it was left as {outer.status_at_entry!r}"
        )
