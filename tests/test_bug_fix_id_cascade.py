"""tests/test_bug_fix_id_cascade.py — runaway ``BUG-FIX-`` prefix cascade.

Observed in a real run (2026-07-22): a synthetic fix task committed by
BugFixLoop becomes a DONE task carrying the same acceptance check as the task
it repaired. ``controller._check_regressions`` re-runs every DONE task's check
after every later commit, so an unresolved regression made the *fix task*
regress too — and ids were derived from that fix task's own id, producing a
new generation on every commit:

    AUTO-T168
    BUG-FIX-AUTO-T168
    BUG-FIX-BUG-FIX-AUTO-T168
    ...

Each generation had a brand-new ticket id, so the "already fixed" /
"already deferred" short-circuits never fired, and the ticket *filename* grew
8 chars per generation until::

    OSError: [Errno 36] File name too long

killed the whole run.

Two independent defences are asserted here:
  1. id canonicalisation in bug_fix_loop (the cascade never starts), and
  2. a hard length cap in safe_filename_component (no id can ever again
     produce ENAMETOOLONG, whatever generates it).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from unittest.mock import MagicMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.bug_fix_loop import (
    BugFixLoop, MAX_FIX_ATTEMPTS, _root_task_id,
)
from tools.auto.state import StateStore
from tools.auto.state import STATUS_BLOCKED, STATUS_DONE, STATUS_TODO, make_task
from tools.auto.ticket_store import make_ticket, make_ticket_store
from tools.auto.utils import safe_filename_component, atomic_write_text


@dataclass
class FakeExecResult:
    passed:    bool = False
    exit_code: int = 4
    stdout:    str = "FAILED"
    stderr:    str = ""
    traceback: str = ""
    timed_out: bool = False


@dataclass
class FakeOuterResult:
    task_id:        str = "BUG-FIX-AUTO-T168"
    passed:         bool = False
    exhausted:      bool = True
    rounds_used:    int = 3
    feedback_files: list = dc_field(default_factory=list)

    def knowledge(self) -> str:
        return "still failing"


def _task(tid: str) -> dict:
    return {
        "id": tid,
        "title": "hc java opts casing",
        "instruction": "fix it",
        "target_files": ["roles/hc/defaults/main.yml"],
        "acceptance_check": "pytest tests/test_config_validation.py -q",
    }


def _bfl(tmp_path: Path, outer_result: FakeOuterResult):
    state = StateStore(tmp_path / ".agent")
    state.initialise("fix regressions", tmp_path)
    tickets = make_ticket_store(tmp_path / ".agent")
    outer = MagicMock()
    outer.run_task.return_value = outer_result
    cos = MagicMock()
    cos.commit.return_value = "abc123def456"
    return BugFixLoop(outer, cos, tickets, state), tickets, state


# ── 1. id canonicalisation ───────────────────────────────────────────────────

class TestRootTaskId:
    def test_plain_id_unchanged(self):
        assert _root_task_id("AUTO-T168") == "AUTO-T168"

    def test_single_prefix_stripped(self):
        assert _root_task_id("BUG-FIX-AUTO-T168") == "AUTO-T168"

    def test_repeated_prefixes_all_stripped(self):
        assert _root_task_id("BUG-FIX-" * 25 + "AUTO-T168") == "AUTO-T168"

    def test_bare_prefix_does_not_yield_empty(self):
        assert _root_task_id("BUG-FIX-") == "BUG-FIX-"

    def test_empty_id_safe(self):
        assert _root_task_id("") == "UNKNOWN"


class TestNoIdGrowth:
    def test_regression_in_fix_task_reuses_root_ticket(self, tmp_path):
        bfl, _tickets, _state = _bfl(tmp_path, FakeOuterResult())
        result = bfl.handle_regression(
            _task("BUG-FIX-AUTO-T168"), FakeExecResult(), base_dir=tmp_path
        )
        # NOT "BUG-BUG-FIX-AUTO-T168"
        assert result.ticket_id == "BUG-AUTO-T168"
        assert result.fix_task_id == "BUG-FIX-AUTO-T168"

    def test_deferred_root_ticket_short_circuits_fix_task_regression(self, tmp_path):
        """The cascade's actual kill-switch: generation 2 hits the dedup."""
        bfl, tickets, _state = _bfl(tmp_path, FakeOuterResult())

        # Generation 1: original task regresses, fix exhausts → deferred.
        r1 = bfl.handle_regression(
            _task("AUTO-T168"), FakeExecResult(), base_dir=tmp_path
        )
        assert r1.exhausted is True
        assert tickets.get("BUG-AUTO-T168")["status"] == "deferred"
        calls_after_gen1 = bfl._outer.run_task.call_count

        # Generation 2: the synthetic fix task now "regresses" as well.
        r2 = bfl.handle_regression(
            _task("BUG-FIX-AUTO-T168"), FakeExecResult(), base_dir=tmp_path
        )
        assert r2.skipped is True
        assert r2.ticket_id == "BUG-AUTO-T168"
        # No further expensive OuterLoop work was burned.
        assert bfl._outer.run_task.call_count == calls_after_gen1

    def test_no_extra_ticket_files_created(self, tmp_path):
        bfl, _tickets, _state = _bfl(tmp_path, FakeOuterResult())
        for tid in ("AUTO-T168", "BUG-FIX-AUTO-T168",
                    "BUG-FIX-BUG-FIX-AUTO-T168"):
            bfl.handle_regression(_task(tid), FakeExecResult(), base_dir=tmp_path)
        files = sorted(p.name for p in (tmp_path / ".agent" / "tickets").glob("*.json"))
        assert files == ["BUG-AUTO-T168.json"]


# ── 2. filename length cap ───────────────────────────────────────────────────

class TestFilenameLengthCap:
    def test_short_name_untouched(self):
        assert safe_filename_component("BUG-AUTO-T168") == "BUG-AUTO-T168"

    def test_long_name_capped(self):
        long_id = "BUG-" + "BUG-FIX-" * 30 + "AUTO-T169"
        out = safe_filename_component(long_id)
        assert len(out) <= 200

    def test_capping_is_deterministic(self):
        long_id = "BUG-" + "BUG-FIX-" * 30 + "AUTO-T169"
        assert safe_filename_component(long_id) == safe_filename_component(long_id)

    def test_capping_avoids_collisions(self):
        a = "BUG-" + "BUG-FIX-" * 30 + "AUTO-T169"
        b = "BUG-" + "BUG-FIX-" * 30 + "AUTO-T170"
        assert safe_filename_component(a) != safe_filename_component(b)

    def test_ticket_write_survives_absurd_id(self, tmp_path):
        """The exact crash: Errno 36 out of atomic_write_text."""
        tickets = make_ticket_store(tmp_path / ".agent")
        absurd = "BUG-" + "BUG-FIX-" * 40 + "AUTO-T169"
        path = tickets.path(absurd)
        atomic_write_text(path, "{}")          # must not raise OSError(36)
        assert path.exists()
        assert len(path.name) < 255


# ── 3. Bounded state machine: every path must terminate ──────────────────────

class TestEveryBranchTerminates:
    """The invariant: handle_regression must never leave a ticket in a state
    that neither short-circuits nor progresses. _check_regressions re-enters
    after EVERY later commit, so a non-terminal state is an unbounded
    OuterLoop (LLM budget) leak even when it no longer crashes."""

    @pytest.mark.parametrize("outer_kwargs,label", [
        ({"passed": True},                    "4a passed"),
        ({"passed": False, "exhausted": True}, "4b exhausted"),
        ({"passed": False, "exhausted": False}, "4c no verdict"),
    ])
    def test_outer_loop_calls_are_bounded(self, tmp_path, outer_kwargs, label):
        bfl, tickets, _ = _bfl(tmp_path, FakeOuterResult(**outer_kwargs))
        tid = "AUTO-T168"
        for _ in range(12):          # simulate 12 subsequent commits
            r = bfl.handle_regression(_task(tid), FakeExecResult(), base_dir=tmp_path)
            tid = r.fix_task_id
        assert bfl._outer.run_task.call_count <= MAX_FIX_ATTEMPTS, (
            f"{label}: unbounded OuterLoop work "
            f"({bfl._outer.run_task.call_count} calls)"
        )
        assert tickets.get("BUG-AUTO-T168")["status"] in {
            "deferred", "verification-failed",
        }

    def test_partial_failure_defers_after_attempts_spent(self, tmp_path):
        """Branch 4c: previously left 'in-progress' forever with no gate."""
        bfl, tickets, _ = _bfl(tmp_path, FakeOuterResult(passed=False, exhausted=False))
        for _ in range(MAX_FIX_ATTEMPTS):
            bfl.handle_regression(_task("AUTO-T168"), FakeExecResult(), base_dir=tmp_path)
        assert tickets.get("BUG-AUTO-T168")["status"] == "deferred"

        calls = bfl._outer.run_task.call_count
        r = bfl.handle_regression(_task("AUTO-T168"), FakeExecResult(), base_dir=tmp_path)
        assert r.skipped is True
        assert bfl._outer.run_task.call_count == calls

    def test_attempt_counter_claimed_before_outer_loop_runs(self, tmp_path):
        """A run killed mid-OuterLoop must not resume with a free attempt."""
        seen = {}
        bfl, tickets, _ = _bfl(tmp_path, FakeOuterResult(passed=False, exhausted=False))
        def spy(task, base_dir):
            seen["attempts"] = tickets.get("BUG-AUTO-T168")["fix_attempts"]
            return FakeOuterResult(passed=False, exhausted=False)
        bfl._outer.run_task.side_effect = spy
        bfl.handle_regression(_task("AUTO-T168"), FakeExecResult(), base_dir=tmp_path)
        assert seen["attempts"] == 1


class TestVerificationFailure:
    def test_fixed_but_still_failing_escalates(self, tmp_path):
        bfl, tickets, _ = _bfl(tmp_path, FakeOuterResult(passed=True))
        bfl.handle_regression(_task("AUTO-T168"), FakeExecResult(), base_dir=tmp_path)
        assert tickets.get("BUG-AUTO-T168")["status"] == "fixed"

        r = bfl.handle_regression(_task("AUTO-T168"), FakeExecResult(), base_dir=tmp_path)
        assert r.fixed is False
        assert r.verification_failed is True
        assert tickets.get("BUG-AUTO-T168")["status"] == "verification-failed"
        assert "VERIFICATION FAILED" in r.summary()

    def test_escalation_does_not_rerun_outer_loop(self, tmp_path):
        bfl, tickets, _ = _bfl(tmp_path, FakeOuterResult(passed=True))
        bfl.handle_regression(_task("AUTO-T168"), FakeExecResult(), base_dir=tmp_path)
        calls = bfl._outer.run_task.call_count
        bfl.handle_regression(_task("AUTO-T168"), FakeExecResult(), base_dir=tmp_path)
        assert bfl._outer.run_task.call_count == calls

    def test_verification_failed_is_terminal(self, tmp_path):
        bfl, tickets, _ = _bfl(tmp_path, FakeOuterResult(passed=True))
        for _ in range(4):
            r = bfl.handle_regression(_task("AUTO-T168"), FakeExecResult(), base_dir=tmp_path)
        assert r.skipped is True
        assert r.verification_failed is True
        assert bfl._outer.run_task.call_count == 1

    def test_operator_reset_to_open_allows_retry(self, tmp_path):
        """'open' is the explicit operator-reset signal and is never gated."""
        bfl, tickets, _ = _bfl(tmp_path, FakeOuterResult(passed=False, exhausted=True))
        bfl.handle_regression(_task("AUTO-T168"), FakeExecResult(), base_dir=tmp_path)
        assert tickets.get("BUG-AUTO-T168")["status"] == "deferred"
        calls = bfl._outer.run_task.call_count

        tickets.update("BUG-AUTO-T168", status="open")
        bfl.handle_regression(_task("AUTO-T168"), FakeExecResult(), base_dir=tmp_path)
        assert bfl._outer.run_task.call_count == calls + 1


class TestTicketLinkage:
    def test_linked_task_is_canonical_root(self, tmp_path):
        bfl, tickets, _ = _bfl(tmp_path, FakeOuterResult(passed=False, exhausted=True))
        bfl.handle_regression(
            _task("BUG-FIX-AUTO-T168"), FakeExecResult(), base_dir=tmp_path
        )
        assert tickets.get("BUG-AUTO-T168")["linked_task"] == "AUTO-T168"
        assert tickets.list_by_task("AUTO-T168")


# ── 4. Defects found reviewing the state-machine fix itself ──────────────────

class TestControllerSkipKeepsCoverage:
    """The BUG-FIX-* skip must never drop the last check covering a command.

    The first version of the filter only asked whether the root task EXISTED.
    But the root is only actually re-checked if it is DONE and is not the
    task just committed — so when the root had been re-planned or reset, the
    root was excluded AND its fix task was skipped as redundant, and the
    acceptance check ran nowhere at all.
    """

    @staticmethod
    def _kept(all_tasks, just_committed_id="OTHER"):
        """Which tasks does the REAL _check_regressions actually re-check?

        This drives AutoController._check_regressions itself and reports the
        tasks it handed to the executor.  An earlier version of this helper
        re-implemented the filter inside the test file, which made every
        assertion below tautological: it passed identically on fixed and
        unfixed production code, so it could not have caught the bug it was
        written for.  Never re-implement the logic under test.
        """
        import configparser
        from unittest.mock import MagicMock
        from tools.auto.controller import AutoController

        state = MagicMock()
        state.all_tasks.return_value = all_tasks

        stub = MagicMock()
        stub.state = state
        stub.is_runtime_exceeded.return_value = False

        executor = MagicMock()
        executor.run.return_value = MagicMock(passed=True)   # no regressions

        AutoController._check_regressions(
            stub, just_committed_id, executor, MagicMock()
        )
        return [c.args[0]["id"] for c in executor.run.call_args_list]

    CHK = "pytest tests/test_x.py -q"

    def test_redundant_fix_task_skipped_when_root_is_done(self):
        from tools.auto.state import STATUS_DONE
        tasks = [{"id": "AUTO-T1", "status": STATUS_DONE, "acceptance_check": self.CHK},
                 {"id": "BUG-FIX-AUTO-T1", "status": STATUS_DONE, "acceptance_check": self.CHK}]
        assert self._kept(tasks) == ["AUTO-T1"]

    def test_fix_task_kept_when_root_not_done(self):
        from tools.auto.state import STATUS_DONE
        tasks = [{"id": "AUTO-T1", "status": "todo", "acceptance_check": self.CHK},
                 {"id": "BUG-FIX-AUTO-T1", "status": STATUS_DONE, "acceptance_check": self.CHK}]
        assert self._kept(tasks) == ["BUG-FIX-AUTO-T1"], "coverage silently dropped"

    def test_fix_task_kept_when_root_is_the_just_committed_task(self):
        from tools.auto.state import STATUS_DONE
        tasks = [{"id": "AUTO-T1", "status": STATUS_DONE, "acceptance_check": self.CHK},
                 {"id": "BUG-FIX-AUTO-T1", "status": STATUS_DONE, "acceptance_check": self.CHK}]
        assert self._kept(tasks, just_committed_id="AUTO-T1") == ["BUG-FIX-AUTO-T1"]

    def test_fix_task_kept_when_checks_differ(self):
        from tools.auto.state import STATUS_DONE
        tasks = [{"id": "AUTO-T1", "status": STATUS_DONE, "acceptance_check": self.CHK},
                 {"id": "BUG-FIX-AUTO-T1", "status": STATUS_DONE,
                  "acceptance_check": "pytest tests/test_other.py -q"}]
        assert self._kept(tasks) == ["AUTO-T1", "BUG-FIX-AUTO-T1"]


class TestBookkeepingWritesAreNonFatal:
    """A vanished ticket file must not kill the run from a status write."""

    def test_deleted_ticket_during_fix_does_not_raise(self, tmp_path):
        bfl, tickets, _ = _bfl(tmp_path, FakeOuterResult(passed=True))

        def delete_then_pass(task, base_dir):
            tickets.delete("BUG-AUTO-T168")     # operator cleanup mid-fix
            return FakeOuterResult(passed=True)

        bfl._outer.run_task.side_effect = delete_then_pass
        r = bfl.handle_regression(_task("AUTO-T168"), FakeExecResult(), base_dir=tmp_path)
        assert r.fixed is True

    def test_deleted_ticket_on_exhaustion_does_not_raise(self, tmp_path):
        bfl, tickets, _ = _bfl(tmp_path, FakeOuterResult(passed=False, exhausted=True))

        def delete_then_exhaust(task, base_dir):
            tickets.delete("BUG-AUTO-T168")
            return FakeOuterResult(passed=False, exhausted=True)

        bfl._outer.run_task.side_effect = delete_then_exhaust
        r = bfl.handle_regression(_task("AUTO-T168"), FakeExecResult(), base_dir=tmp_path)
        assert r.exhausted is True


class TestAttemptCounterIsRobust:
    """fix_attempts is persisted JSON — it may be absent, stale, or edited."""

    @pytest.mark.parametrize("raw,expected", [
        (None, 0), (0, 0), (2, 2), ("3", 3), ("", 0), ("abc", 0), (-5, 0), ([], 0),
    ])
    def test_unreadable_counter_degrades_to_zero(self, raw, expected):
        assert BugFixLoop._attempts_of({"fix_attempts": raw}) == expected

    def test_missing_field_on_legacy_ticket(self):
        assert BugFixLoop._attempts_of({}) == 0
        assert BugFixLoop._attempts_of(None) == 0

    def test_hand_edited_garbage_does_not_crash_the_gate(self, tmp_path):
        bfl, tickets, _ = _bfl(tmp_path, FakeOuterResult(passed=False, exhausted=False))
        bfl.handle_regression(_task("AUTO-T168"), FakeExecResult(), base_dir=tmp_path)
        tickets.update("BUG-AUTO-T168", fix_attempts="corrupted")
        r = bfl.handle_regression(_task("AUTO-T168"), FakeExecResult(), base_dir=tmp_path)
        assert r is not None


class TestFixTaskDoesNotSurviveInTheQueue:
    """A deferred regression must not be re-run via the main task queue.

    _build_fix_task registers BUG-FIX-* with the default STATUS_TODO and only
    the success path clears it.  resume_info() treats everything that is not
    DONE or BLOCKED as pending, so a resumed run pulled the fix task out of
    the queue and spent a fresh full OuterLoop budget on the regression that
    had just been deferred as unfixable — bypassing the ticket short-circuit,
    which only guards re-entry through _check_regressions.
    """

    @staticmethod
    def _pending_ids(state):
        return [t["id"] for t in state.resume_info()["pending"]]

    def test_exhausted_fix_task_not_pending_on_resume(self, tmp_path):
        bfl, _t, state = _bfl(tmp_path, FakeOuterResult(passed=False, exhausted=True))
        bfl.handle_regression(_task("AUTO-T168"), FakeExecResult(), base_dir=tmp_path)
        assert "BUG-FIX-AUTO-T168" not in self._pending_ids(state)

    def test_no_verdict_fix_task_not_pending_on_resume(self, tmp_path):
        bfl, _t, state = _bfl(tmp_path, FakeOuterResult(passed=False, exhausted=False))
        bfl.handle_regression(_task("AUTO-T168"), FakeExecResult(), base_dir=tmp_path)
        assert "BUG-FIX-AUTO-T168" not in self._pending_ids(state)

    def test_operator_reset_re_registers_the_fix_task(self, tmp_path):
        """Parking must not make a deliberate retry impossible."""
        bfl, tickets, state = _bfl(tmp_path, FakeOuterResult(passed=False, exhausted=True))
        bfl.handle_regression(_task("AUTO-T168"), FakeExecResult(), base_dir=tmp_path)
        assert "BUG-FIX-AUTO-T168" not in self._pending_ids(state)

        tickets.update("BUG-AUTO-T168", status="open")
        bfl._outer.run_task.return_value = FakeOuterResult(passed=True)
        r = bfl.handle_regression(_task("AUTO-T168"), FakeExecResult(), base_dir=tmp_path)
        assert r.fixed is True


# ── 5. Defects found reviewing the parking / budget fix itself ───────────────

class TestOperatorResetRestoresBudget:
    """Resetting a ticket to "open" must restore a FULL attempt budget.

    fix_attempts persists across the reset, so carrying it over meant the
    retry got a single attempt before deferring again — and logged the
    nonsense "attempt 3/2" — no matter how many times the operator reset it.
    A ticket goes to "in-progress" the moment its attempt is claimed, so an
    existing ticket sitting at "open" can only be a deliberate reset.
    """

    def test_reset_grants_full_budget_again(self, tmp_path):
        bfl, tickets, _ = _bfl(tmp_path, FakeOuterResult(passed=False, exhausted=False))
        for _ in range(MAX_FIX_ATTEMPTS):
            bfl.handle_regression(_task("AUTO-T168"), FakeExecResult(), base_dir=tmp_path)
        assert tickets.get("BUG-AUTO-T168")["status"] == "deferred"

        tickets.update("BUG-AUTO-T168", status="open")
        base = bfl._outer.run_task.call_count
        for _ in range(MAX_FIX_ATTEMPTS + 3):
            bfl.handle_regression(_task("AUTO-T168"), FakeExecResult(), base_dir=tmp_path)
        assert bfl._outer.run_task.call_count - base == MAX_FIX_ATTEMPTS

    def test_reset_is_still_bounded(self, tmp_path):
        """Restoring the budget must not reopen the unbounded path."""
        bfl, tickets, _ = _bfl(tmp_path, FakeOuterResult(passed=False, exhausted=False))
        for _ in range(20):
            bfl.handle_regression(_task("AUTO-T168"), FakeExecResult(), base_dir=tmp_path)
        assert bfl._outer.run_task.call_count == MAX_FIX_ATTEMPTS
        assert tickets.get("BUG-AUTO-T168")["status"] == "deferred"

    def test_fresh_ticket_unaffected(self, tmp_path):
        bfl, tickets, _ = _bfl(tmp_path, FakeOuterResult(passed=False, exhausted=True))
        bfl.handle_regression(_task("AUTO-T168"), FakeExecResult(), base_dir=tmp_path)
        assert tickets.get("BUG-AUTO-T168")["fix_attempts"] == 1


class TestTerminalGatesAlsoParkFixTask:
    """Parking on 4b/4c was not enough — the early-return gates matter too.

    If a run is killed after the attempt is claimed but before OuterLoop
    returns a verdict, the fix task is left TODO.  On resume the ticket gate
    fires and returns early, so 4b/4c never run — and without parking here
    the fix task stays pending and the main queue re-runs the regression
    that was just deferred, bypassing the short-circuit entirely.
    """

    @staticmethod
    def _crash_then_resume(tmp_path, resume_outer):
        state   = StateStore(tmp_path / ".agent")
        state.initialise("fix regressions", tmp_path)
        tickets = make_ticket_store(tmp_path / ".agent")
        cos     = MagicMock()

        class Boom(Exception):
            pass

        dying = MagicMock()
        dying.run_task.side_effect = Boom("run killed mid-OuterLoop")
        try:
            BugFixLoop(dying, cos, tickets, state, max_fix_attempts=1) \
                .handle_regression(_task("AUTO-T168"), FakeExecResult(), base_dir=tmp_path)
        except Boom:
            pass
        # An in-process exception now parks the task on its way out, so force
        # it back to model the harder case the gate must still cover: SIGKILL
        # / power loss, where no Python runs and the task is left TODO on disk
        # with no opportunity to clean up.
        state.set_task_status("BUG-FIX-AUTO-T168", "todo")

        outer = MagicMock()
        outer.run_task.return_value = resume_outer
        bfl = BugFixLoop(outer, cos, tickets, state, max_fix_attempts=1)
        result = bfl.handle_regression(
            _task("AUTO-T168"), FakeExecResult(), base_dir=tmp_path
        )
        pending = [t["id"] for t in state.resume_info()["pending"]]
        return result, pending, tickets, outer

    def test_attempts_exhausted_gate_parks(self, tmp_path):
        result, pending, tickets, outer = self._crash_then_resume(
            tmp_path, FakeOuterResult(passed=True)
        )
        assert result.skipped is True
        assert tickets.get("BUG-AUTO-T168")["status"] == "deferred"
        assert "BUG-FIX-AUTO-T168" not in pending
        outer.run_task.assert_not_called()

    def test_deferred_gate_parks(self, tmp_path):
        state   = StateStore(tmp_path / ".agent")
        state.initialise("fix regressions", tmp_path)
        tickets = make_ticket_store(tmp_path / ".agent")
        outer   = MagicMock()
        outer.run_task.return_value = FakeOuterResult(passed=False, exhausted=True)
        bfl = BugFixLoop(outer, MagicMock(), tickets, state)
        bfl.handle_regression(_task("AUTO-T168"), FakeExecResult(), base_dir=tmp_path)
        # Un-park by hand to model a fix task revived by an external edit.
        state.set_task_status("BUG-FIX-AUTO-T168", "todo")
        bfl.handle_regression(_task("AUTO-T168"), FakeExecResult(), base_dir=tmp_path)
        pending = [t["id"] for t in state.resume_info()["pending"]]
        assert "BUG-FIX-AUTO-T168" not in pending

    def test_parking_absent_task_is_a_quiet_noop(self, tmp_path):
        """Terminal gates usually run with no fix task in state at all."""
        bfl, tickets, state = _bfl(tmp_path, FakeOuterResult(passed=True))
        tickets.create(make_ticket(
            id="BUG-AUTO-T999", type="bug", linked_task="AUTO-T999",
            title="t", body="b", status="deferred",
        ))
        r = bfl.handle_regression(_task("AUTO-T999"), FakeExecResult(), base_dir=tmp_path)
        assert r.skipped is True
        assert state.get_task("BUG-FIX-AUTO-T999") is None

    def test_parking_does_not_disturb_a_committed_fix_task(self, tmp_path):
        """A DONE fix task must stay DONE, not be dragged back to BLOCKED."""
        bfl, tickets, state = _bfl(tmp_path, FakeOuterResult(passed=True))
        bfl.handle_regression(_task("AUTO-T168"), FakeExecResult(), base_dir=tmp_path)
        state.set_task_status("BUG-FIX-AUTO-T168", STATUS_DONE)
        bfl.handle_regression(_task("AUTO-T168"), FakeExecResult(), base_dir=tmp_path)
        assert state.get_task("BUG-FIX-AUTO-T168")["status"] == STATUS_DONE


# ── 6. Defects found by randomised state-machine fuzzing ────────────────────

class TestPassedButNotCommitted:
    """CommitOnSuccess.commit() can return None — "passed" is not "committed".

    commit() returns None on GitError AND when nothing was staged, and only
    calls set_task_status(DONE) when a sha actually comes back.  So a fix
    that passed but produced no commit left the fix task TODO — pending —
    for the next run's MAIN queue to execute outside this loop's accounting,
    while the ticket was marked "fixed" for a repair that exists only in the
    working tree, or nowhere.

    Not exotic: a flaky check that regresses and then passes again without
    the coder changing anything yields an empty diff, hence no sha.  A
    failing pre-commit hook, detached HEAD or missing git identity do too.
    """

    @staticmethod
    def _bfl_no_commit(tmp_path, max_attempts=MAX_FIX_ATTEMPTS):
        state = StateStore(tmp_path / ".agent")
        state.initialise("fix regressions", tmp_path)
        tickets = make_ticket_store(tmp_path / ".agent")
        outer = MagicMock()
        outer.run_task.return_value = FakeOuterResult(passed=True)
        cos = MagicMock()
        cos.commit.return_value = None          # empty diff / GitError
        return BugFixLoop(outer, cos, tickets, state, max_fix_attempts=max_attempts), \
               tickets, state, outer

    def test_ticket_not_marked_fixed_without_a_commit(self, tmp_path):
        bfl, tickets, _s, _o = self._bfl_no_commit(tmp_path)
        r = bfl.handle_regression(_task("AUTO-T168"), FakeExecResult(), base_dir=tmp_path)
        assert r.fixed is False
        assert tickets.get("BUG-AUTO-T168")["status"] != "fixed"

    def test_fix_task_not_left_pending(self, tmp_path):
        bfl, _t, state, _o = self._bfl_no_commit(tmp_path)
        bfl.handle_regression(_task("AUTO-T168"), FakeExecResult(), base_dir=tmp_path)
        pending = [t["id"] for t in state.resume_info()["pending"]]
        assert "BUG-FIX-AUTO-T168" not in pending

    def test_bounded_then_deferred(self, tmp_path):
        bfl, tickets, _s, outer = self._bfl_no_commit(tmp_path)
        for _ in range(12):
            bfl.handle_regression(_task("AUTO-T168"), FakeExecResult(), base_dir=tmp_path)
        assert outer.run_task.call_count == MAX_FIX_ATTEMPTS
        assert tickets.get("BUG-AUTO-T168")["status"] == "deferred"

    def test_nothing_staged_is_a_SUCCESS_not_a_failure(self, tmp_path):
        """commit() returns None for two different outcomes — don't conflate.

        On an empty diff, CommitOnSuccess deliberately marks the task DONE
        with commit="" and returns None (commit_on_success.py, the `else`
        after `if sha:`).  The acceptance check passed and the tree is
        already correct — that is exactly what a flaky regression resolving
        itself looks like, and it is a success.  Only a GitError returns None
        while leaving the task unsettled.
        """
        state = StateStore(tmp_path / ".agent")
        state.initialise("fix regressions", tmp_path)
        tickets = make_ticket_store(tmp_path / ".agent")
        outer = MagicMock()
        outer.run_task.return_value = FakeOuterResult(passed=True)
        cos = MagicMock()

        def nothing_staged(task_, result_):
            state.set_task_status(task_["id"], STATUS_DONE, commit="")
            return None

        cos.commit.side_effect = nothing_staged
        bfl = BugFixLoop(outer, cos, tickets, state)
        r = bfl.handle_regression(_task("AUTO-T168"), FakeExecResult(), base_dir=tmp_path)

        assert r.fixed is True
        assert tickets.get("BUG-AUTO-T168")["status"] == "fixed"
        assert outer.run_task.call_count == 1      # not retried as inconclusive

    def test_git_error_is_still_treated_as_failure(self, tmp_path):
        """The other None: commit() returns early, nothing is settled."""
        bfl, tickets, state, _o = self._bfl_no_commit(tmp_path)
        r = bfl.handle_regression(_task("AUTO-T168"), FakeExecResult(), base_dir=tmp_path)
        assert r.fixed is False
        assert state.get_task("BUG-FIX-AUTO-T168")["status"] != STATUS_DONE
        assert tickets.get("BUG-AUTO-T168")["status"] != "fixed"

    def test_real_commit_still_marks_fixed(self, tmp_path):
        """The guard must not break the ordinary success path."""
        bfl, tickets, _ = _bfl(tmp_path, FakeOuterResult(passed=True))
        r = bfl.handle_regression(_task("AUTO-T168"), FakeExecResult(), base_dir=tmp_path)
        assert r.fixed is True
        assert r.commit_hash
        assert tickets.get("BUG-AUTO-T168")["status"] == "fixed"


class TestCrashDuringOuterLoop:
    """An exception out of OuterLoop must not leak the fix task."""

    class Boom(Exception):
        pass

    def _crashing(self, tmp_path, max_attempts=MAX_FIX_ATTEMPTS):
        state = StateStore(tmp_path / ".agent")
        state.initialise("fix regressions", tmp_path)
        tickets = make_ticket_store(tmp_path / ".agent")
        outer = MagicMock()
        outer.run_task.side_effect = self.Boom("LLM timeout / OOM / API error")
        return BugFixLoop(outer, MagicMock(), tickets, state,
                          max_fix_attempts=max_attempts), tickets, state

    def test_exception_propagates(self, tmp_path):
        bfl, _t, _s = self._crashing(tmp_path)
        with pytest.raises(self.Boom):
            bfl.handle_regression(_task("AUTO-T168"), FakeExecResult(), base_dir=tmp_path)

    def test_fix_task_parked_not_pending(self, tmp_path):
        bfl, _t, state = self._crashing(tmp_path)
        with pytest.raises(self.Boom):
            bfl.handle_regression(_task("AUTO-T168"), FakeExecResult(), base_dir=tmp_path)
        pending = [t["id"] for t in state.resume_info()["pending"]]
        assert "BUG-FIX-AUTO-T168" not in pending

    def test_attempt_still_charged(self, tmp_path):
        """The crashed attempt must not be free, or crashes become a loop."""
        bfl, tickets, _s = self._crashing(tmp_path)
        with pytest.raises(self.Boom):
            bfl.handle_regression(_task("AUTO-T168"), FakeExecResult(), base_dir=tmp_path)
        assert tickets.get("BUG-AUTO-T168")["fix_attempts"] == 1

    def test_crash_on_last_attempt_settles_the_ticket(self, tmp_path):
        """Otherwise .agent/tickets/ misreports live work during an incident."""
        bfl, tickets, _s = self._crashing(tmp_path, max_attempts=1)
        with pytest.raises(self.Boom):
            bfl.handle_regression(_task("AUTO-T168"), FakeExecResult(), base_dir=tmp_path)
        assert tickets.get("BUG-AUTO-T168")["status"] == "deferred"

    def test_crash_with_budget_left_stays_retryable(self, tmp_path):
        bfl, tickets, _s = self._crashing(tmp_path, max_attempts=3)
        with pytest.raises(self.Boom):
            bfl.handle_regression(_task("AUTO-T168"), FakeExecResult(), base_dir=tmp_path)
        assert tickets.get("BUG-AUTO-T168")["status"] == "in-progress"

        bfl._outer.run_task.side_effect = None
        bfl._outer.run_task.return_value = FakeOuterResult(passed=True)
        r = bfl.handle_regression(_task("AUTO-T168"), FakeExecResult(), base_dir=tmp_path)
        assert r.fixed is True


# ── 7. No-git mode ──────────────────────────────────────────────────────────

class TestNoGitMode:
    """A git failure is explicitly non-fatal — BugFixLoop must honour that.

    Controller._setup_git catches GitError/OSError, sets self.git = None and
    logs "continuing without git": Epic A is documented as usable without
    git, with commits simply not happening.  It then derives
    commit_helper = None and passes that straight to make_bug_fix_loop.

    Two separate breakages followed from that None:
      * make_bug_fix_loop treated it as "not supplied" and rebuilt one,
        re-running the SAME make_git_manager call the controller had just
        guarded — unguarded this time, so the swallowed GitError was raised
        again and aborted the run at construction.
      * BugFixLoop then dereferenced self._cos unconditionally, so even a
        successfully-constructed no-git loop died with AttributeError on the
        first regression it tried to fix.
    """

    def test_loop_constructs_without_git(self, tmp_path, monkeypatch):
        import configparser
        from tools.auto import bug_fix_loop as bfl_mod
        from tools.auto.git_manager import GitError

        state = StateStore(tmp_path / ".agent")
        state.initialise("g", tmp_path)
        cfg = configparser.ConfigParser()
        cfg.add_section("auto")

        def boom(*a, **kw):
            raise GitError("git init failed")

        monkeypatch.setattr(
            "tools.auto.commit_on_success.make_commit_on_success", boom
        )
        loop = bfl_mod.make_bug_fix_loop(
            cfg, tmp_path, state, outer_loop=object(), commit_on_success=None
        )
        assert loop is not None
        assert loop._cos is None

    def test_fix_succeeds_without_a_commit_helper(self, tmp_path):
        state = StateStore(tmp_path / ".agent")
        state.initialise("fix regressions", tmp_path)
        tickets = make_ticket_store(tmp_path / ".agent")
        outer = MagicMock()
        outer.run_task.return_value = FakeOuterResult(passed=True)

        bfl = BugFixLoop(outer, None, tickets, state)     # no-git
        r = bfl.handle_regression(_task("AUTO-T168"), FakeExecResult(), base_dir=tmp_path)

        assert r.fixed is True
        assert r.commit_hash is None
        assert tickets.get("BUG-AUTO-T168")["status"] == "fixed"

    def test_fix_task_settled_not_left_pending_without_git(self, tmp_path):
        state = StateStore(tmp_path / ".agent")
        state.initialise("fix regressions", tmp_path)
        tickets = make_ticket_store(tmp_path / ".agent")
        outer = MagicMock()
        outer.run_task.return_value = FakeOuterResult(passed=True)

        BugFixLoop(outer, None, tickets, state).handle_regression(
            _task("AUTO-T168"), FakeExecResult(), base_dir=tmp_path
        )
        assert state.get_task("BUG-FIX-AUTO-T168")["status"] == STATUS_DONE
        pending = [t["id"] for t in state.resume_info()["pending"]]
        assert "BUG-FIX-AUTO-T168" not in pending

    def test_no_git_still_bounded_on_failure(self, tmp_path):
        state = StateStore(tmp_path / ".agent")
        state.initialise("fix regressions", tmp_path)
        tickets = make_ticket_store(tmp_path / ".agent")
        outer = MagicMock()
        outer.run_task.return_value = FakeOuterResult(passed=False, exhausted=False)

        bfl = BugFixLoop(outer, None, tickets, state)
        for _ in range(10):
            bfl.handle_regression(_task("AUTO-T168"), FakeExecResult(), base_dir=tmp_path)
        assert outer.run_task.call_count == MAX_FIX_ATTEMPTS


# ── 8. Parking must survive the startup reset ───────────────────────────────

class TestParkingSurvivesStartupReset:
    """Controller._reset_resettable_blocked_tasks was undoing the parking.

    That method resets BLOCKED tasks to TODO at every startup so unmet
    dependencies get re-evaluated, skipping only tasks that have burned all
    their OuterLoop rounds.  A fix task parked by BugFixLoop has usually
    burned none — a run killed mid-fix, or an inconclusive attempt — so it
    was reset on every resume, put straight back into the pending queue, and
    executed by the main loop outside the ticket short-circuit and outside
    the fix_attempts bound.  Parking was undone in exactly the cases it
    exists for.

    Fix tasks carry no depends_on, so the dependency case cannot apply to
    them; BLOCKED on a BUG-FIX-* task always means parked or round-exhausted,
    and neither benefits from a reset.
    """

    @staticmethod
    def _real_startup_reset(state):
        """Invoke the REAL AutoController._reset_resettable_blocked_tasks.

        The first version of this helper replayed the method's logic inside
        the test file.  That made the whole class tautological — it passed on
        unfixed production code, which is precisely the failure mode these
        tests exist to rule out.
        """
        import configparser
        from unittest.mock import MagicMock
        from tools.auto.controller import AutoController

        cfg = configparser.ConfigParser()
        cfg.add_section("auto")
        stub = MagicMock()
        stub.state = state
        AutoController._reset_resettable_blocked_tasks(stub, cfg)

    def test_parked_fix_task_stays_parked_across_restart(self, tmp_path):
        bfl, _t, state = _bfl(tmp_path, FakeOuterResult(passed=False, exhausted=False))
        bfl.handle_regression(_task("AUTO-T168"), FakeExecResult(), base_dir=tmp_path)
        assert state.get_task("BUG-FIX-AUTO-T168")["status"] == STATUS_BLOCKED

        self._real_startup_reset(state)

        assert state.get_task("BUG-FIX-AUTO-T168")["status"] == STATUS_BLOCKED
        pending = [t["id"] for t in state.resume_info()["pending"]]
        assert "BUG-FIX-AUTO-T168" not in pending

    def test_parked_after_crash_stays_parked(self, tmp_path):
        """The crash case has burned no rounds, so case 2 would not save it."""
        state = StateStore(tmp_path / ".agent")
        state.initialise("fix regressions", tmp_path)
        tickets = make_ticket_store(tmp_path / ".agent")

        class Boom(Exception):
            pass

        outer = MagicMock()
        outer.run_task.side_effect = Boom("killed mid-fix")
        with pytest.raises(Boom):
            BugFixLoop(outer, MagicMock(), tickets, state).handle_regression(
                _task("AUTO-T168"), FakeExecResult(), base_dir=tmp_path
            )
        self._real_startup_reset(state)
        pending = [t["id"] for t in state.resume_info()["pending"]]
        assert "BUG-FIX-AUTO-T168" not in pending

    def test_ordinary_blocked_task_is_still_reset(self, tmp_path):
        """The dependency case must keep working — don't over-broaden."""
        state = StateStore(tmp_path / ".agent")
        state.initialise("g", tmp_path)
        state.upsert_task(make_task(
            id="AUTO-T9", title="blocked on a dependency", instruction="i",
            target_files=["a.py"], acceptance_check="true",
        ))
        state.set_task_status("AUTO-T9", STATUS_BLOCKED)

        self._real_startup_reset(state)

        assert state.get_task("AUTO-T9")["status"] == STATUS_TODO

    def test_operator_reset_still_revives_the_fix_task(self, tmp_path):
        """Skipping the reset must not make a deliberate retry impossible."""
        bfl, tickets, state = _bfl(tmp_path, FakeOuterResult(passed=False, exhausted=True))
        bfl.handle_regression(_task("AUTO-T168"), FakeExecResult(), base_dir=tmp_path)
        self._real_startup_reset(state)

        tickets.update("BUG-AUTO-T168", status="open")
        bfl._outer.run_task.return_value = FakeOuterResult(passed=True)
        r = bfl.handle_regression(_task("AUTO-T168"), FakeExecResult(), base_dir=tmp_path)
        assert r.fixed is True


# ── 9. The operator retry must be a REAL retry ──────────────────────────────

class TestOperatorRetryIsReal:
    """Restoring the attempt budget is not enough on its own.

    OuterLoop does not read its starting round from task state — it counts
    feedback_round_*.md files on disk (start_round = done_rounds + 1) and
    returns "already exhausted" immediately once that exceeds max_rounds.  So
    after an exhausted fix attempt, every later attempt returned exhausted
    instantly having done NO work, which made the documented operator reset
    (ticket -> "open", which restores the budget) completely inert.

    This is the same trap _reset_resettable_blocked_tasks documents for
    BLOCKED tasks: "a bare status reset never touches" the files OuterLoop
    reads.  The rounds are archived, not deleted, so accumulated feedback
    stays inspectable.
    """

    @staticmethod
    def _exhaust_with_real_feedback_files(tmp_path, rounds=10):
        from tools.auto.utils import highest_completed_round
        state = StateStore(tmp_path / ".agent")
        state.initialise("fix regressions", tmp_path)
        tickets = make_ticket_store(tmp_path / ".agent")
        outer = MagicMock()

        def burn_rounds(task_, base_dir):
            for n in range(1, rounds + 1):
                state.write_task_file(task_["id"], f"feedback_round_{n}.md", "fb")
            return FakeOuterResult(passed=False, exhausted=True)

        outer.run_task.side_effect = burn_rounds
        bfl = BugFixLoop(outer, MagicMock(), tickets, state)
        bfl.handle_regression(_task("AUTO-T168"), FakeExecResult(), base_dir=tmp_path)
        assert highest_completed_round(state.task_dir("BUG-FIX-AUTO-T168")) == rounds
        return bfl, tickets, state

    def test_reset_clears_the_round_counter(self, tmp_path):
        from tools.auto.utils import highest_completed_round
        bfl, tickets, state = self._exhaust_with_real_feedback_files(tmp_path)

        tickets.update("BUG-AUTO-T168", status="open")
        bfl._outer.run_task.side_effect = None
        bfl._outer.run_task.return_value = FakeOuterResult(passed=True)
        bfl.handle_regression(_task("AUTO-T168"), FakeExecResult(), base_dir=tmp_path)

        # start_round = 0 + 1 = 1, so OuterLoop does real work again
        assert highest_completed_round(state.task_dir("BUG-FIX-AUTO-T168")) == 0

    def test_feedback_is_archived_not_destroyed(self, tmp_path):
        bfl, tickets, state = self._exhaust_with_real_feedback_files(tmp_path)
        tickets.update("BUG-AUTO-T168", status="open")
        bfl._outer.run_task.side_effect = None
        bfl._outer.run_task.return_value = FakeOuterResult(passed=True)
        bfl.handle_regression(_task("AUTO-T168"), FakeExecResult(), base_dir=tmp_path)

        tdir = state.task_dir("BUG-FIX-AUTO-T168")
        archives = [p for p in tdir.iterdir() if p.is_dir()
                    and p.name.startswith("previous_attempt_")]
        assert len(archives) == 1
        assert len(list(archives[0].glob("feedback_round_*.md"))) == 10

    def test_ordinary_attempts_do_not_archive(self, tmp_path):
        """Only an operator reset clears rounds — not every in-run attempt."""
        bfl, _t, state = self._exhaust_with_real_feedback_files(tmp_path)
        tdir = state.task_dir("BUG-FIX-AUTO-T168")
        assert not [p for p in tdir.iterdir()
                    if p.is_dir() and p.name.startswith("previous_attempt_")]

    def test_no_feedback_files_is_a_noop(self, tmp_path):
        bfl, tickets, state = _bfl(tmp_path, FakeOuterResult(passed=False, exhausted=True))
        bfl.handle_regression(_task("AUTO-T168"), FakeExecResult(), base_dir=tmp_path)
        tickets.update("BUG-AUTO-T168", status="open")
        bfl._outer.run_task.return_value = FakeOuterResult(passed=True)
        r = bfl.handle_regression(_task("AUTO-T168"), FakeExecResult(), base_dir=tmp_path)
        assert r.fixed is True


# ── 10. Failed fix attempts must not leave dirty residue ────────────────────

class TestFailedFixDiscardsResidue:
    """The documented "Bug 2" hazard, unguarded for fix attempts.

    controller.py already handles it for main tasks: the coder writes its
    candidate into base_dir BEFORE validation, so a task that ends without a
    commit leaves that edit dirty — and commit() stages everything
    (git add -u && git add .), sweeping it into the NEXT successful task's
    commit.  BugFixLoop had no git access at all and never cleaned up, so for
    a fix attempt the swept-in edit is code that FAILED its acceptance check:
    broken work lands silently under an unrelated task's message.
    """

    @staticmethod
    def _bfl_with_spy_git(tmp_path, outer_result, side_effect=None):
        state = StateStore(tmp_path / ".agent")
        state.initialise("fix regressions", tmp_path)
        tickets = make_ticket_store(tmp_path / ".agent")
        git = MagicMock()
        cos = MagicMock()
        cos._git = git
        cos.commit.return_value = None
        outer = MagicMock()
        if side_effect is not None:
            outer.run_task.side_effect = side_effect
        else:
            outer.run_task.return_value = outer_result
        return BugFixLoop(outer, cos, tickets, state), git, tickets, state

    def test_exhausted_attempt_discards(self, tmp_path):
        bfl, git, _t, _s = self._bfl_with_spy_git(
            tmp_path, FakeOuterResult(passed=False, exhausted=True)
        )
        bfl.handle_regression(_task("AUTO-T168"), FakeExecResult(), base_dir=tmp_path)
        git.discard_working_changes.assert_called_once()

    def test_no_verdict_attempt_discards(self, tmp_path):
        bfl, git, _t, _s = self._bfl_with_spy_git(
            tmp_path, FakeOuterResult(passed=False, exhausted=False)
        )
        bfl.handle_regression(_task("AUTO-T168"), FakeExecResult(), base_dir=tmp_path)
        git.discard_working_changes.assert_called_once()

    def test_crash_discards(self, tmp_path):
        class Boom(Exception):
            pass

        bfl, git, _t, _s = self._bfl_with_spy_git(tmp_path, None, side_effect=Boom())
        with pytest.raises(Boom):
            bfl.handle_regression(_task("AUTO-T168"), FakeExecResult(), base_dir=tmp_path)
        git.discard_working_changes.assert_called_once()

    def test_failed_commit_discards(self, tmp_path):
        """passed but GitError — the edits were never recorded anywhere."""
        bfl, git, _t, _s = self._bfl_with_spy_git(
            tmp_path, FakeOuterResult(passed=True)
        )
        bfl.handle_regression(_task("AUTO-T168"), FakeExecResult(), base_dir=tmp_path)
        git.discard_working_changes.assert_called_once()

    def test_successful_fix_does_NOT_discard(self, tmp_path):
        """Committed work must never be thrown away."""
        state = StateStore(tmp_path / ".agent")
        state.initialise("fix regressions", tmp_path)
        tickets = make_ticket_store(tmp_path / ".agent")
        git = MagicMock()
        cos = MagicMock()
        cos._git = git

        def commit(task_, result_):
            state.set_task_status(task_["id"], STATUS_DONE, commit="abc123")
            return "abc123"

        cos.commit.side_effect = commit
        outer = MagicMock()
        outer.run_task.return_value = FakeOuterResult(passed=True)

        r = BugFixLoop(outer, cos, tickets, state).handle_regression(
            _task("AUTO-T168"), FakeExecResult(), base_dir=tmp_path
        )
        assert r.fixed is True
        git.discard_working_changes.assert_not_called()

    def test_nothing_staged_success_does_NOT_discard(self, tmp_path):
        """An empty diff is a success — there is nothing to clean."""
        state = StateStore(tmp_path / ".agent")
        state.initialise("fix regressions", tmp_path)
        tickets = make_ticket_store(tmp_path / ".agent")
        git = MagicMock()
        cos = MagicMock()
        cos._git = git

        def commit(task_, result_):
            state.set_task_status(task_["id"], STATUS_DONE, commit="")
            return None

        cos.commit.side_effect = commit
        outer = MagicMock()
        outer.run_task.return_value = FakeOuterResult(passed=True)

        r = BugFixLoop(outer, cos, tickets, state).handle_regression(
            _task("AUTO-T168"), FakeExecResult(), base_dir=tmp_path
        )
        assert r.fixed is True
        git.discard_working_changes.assert_not_called()

    def test_no_git_mode_is_a_noop(self, tmp_path):
        bfl, _t, state = _bfl(tmp_path, FakeOuterResult(passed=False, exhausted=True))
        bfl._cos = None
        r = bfl.handle_regression(_task("AUTO-T168"), FakeExecResult(), base_dir=tmp_path)
        assert r.exhausted is True
