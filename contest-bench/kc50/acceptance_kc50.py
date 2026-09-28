"""KC-50 (round 94) judge's acceptance suite: a harvest that is already
`REWORK` on its mechanical facts runs no pytest root, and the round's test lock
is held only around the roots.

Written from the ticket's "What must change" §1–3 and its Acceptance list,
through the public contract only: `harvest(..., run_tests=True, test_lock=)`
with a context manager, `facts["tests_run"] == "skipped: <first blocking
code>"`, `rework_message`'s "not run" line, and the runner's `_harvest` with
the real `_TEST_RUNS_LOCK` and `_SUITE_SLOTS`. `run_tests_detail` is patched in
`tools.contest.harvest` — no real pytest root runs. Must be red on the base
(a915288).

H* — the skip in `harvest` (§1) and the lock inside it (§2).
M* — the rework prompt (§3).
R* — the runner: a mechanical harvest never waits behind another agent's
     roots, with the suite slots armed (the shipped default) and off; the
     KC-57 wait and queue depth still reach the verdict of one that queued.
"""
from __future__ import annotations

import dataclasses
import sys
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TESTS_DIR = REPO_ROOT / "tests"
for _p in (str(REPO_ROOT), str(TESTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from test_contest_runner import (  # noqa: E402
    Sandbox, _git, _write, make_config, work_no_test, work_ready,
)
import tools.contest.harvest as harvest_mod  # noqa: E402
import tools.contest.runner as runner_mod  # noqa: E402
from tools.contest.harvest import Harvest, Reason, harvest, rework_message  # noqa: E402

GREEN = "tests ✓ 4 roots"
NOT_RUN = "The test roots were not run: fix the items above first."


@pytest.fixture(autouse=True)
def _clean_queues():
    runner_mod._SUITE_SLOTS.reset()
    yield
    runner_mod._SUITE_SLOTS.reset()


@pytest.fixture
def calls(monkeypatch):
    """`run_tests_detail` recorded, green, instant."""
    seen: list = []

    def fake(cwd, budget_sec=0.0, **_kw):
        seen.append(str(cwd))
        return GREEN, []

    monkeypatch.setattr(harvest_mod, "run_tests_detail", fake)
    return seen


class _Recording:
    """A context manager lock that logs enter/exit, and whether it is held."""

    def __init__(self):
        self.log: list = []
        self.held = False

    def __enter__(self):
        self.log.append("enter")
        self.held = True
        return self

    def __exit__(self, *exc):
        self.log.append("exit")
        self.held = False
        return False


def _one(tmp_path, name="agent-a"):
    sb = Sandbox(tmp_path, [name])
    return sb, sb.ws(name)


def _two_commits(ws):
    work_ready(str(ws.path), "")
    _write(ws.path / "pkg" / "thing.py", "def thing():\n    return 43\n")
    _git(str(ws.path), "commit", "-qam", "second")


# ── H: the skip and the lock in `harvest` ───────────────────────────────────

def test_H1_base_no_row_skips_with_the_first_blocking_code(tmp_path, calls):
    sb, ws = _one(tmp_path)
    h = harvest(ws, sb.ticket_path, run_tests=True)
    assert calls == []
    assert h.verdict == "REWORK"
    assert h.facts["tests_run"] == "skipped: no_progress_row"
    assert "tests_failed" not in [r.code for r in h.reasons]


def test_H2_ready_commit_runs_the_roots_once(tmp_path, calls):
    sb, ws = _one(tmp_path)
    work_ready(str(ws.path), "")
    h = harvest(ws, sb.ticket_path, run_tests=True)
    assert len(calls) == 1
    assert h.verdict == "READY", h.reasons
    assert h.facts["tests_run"] == GREEN


def test_H3_only_off_ticket_still_runs(tmp_path, calls):
    sb, ws = _one(tmp_path)
    _write(ws.path / "notes.txt", "x\n")
    work_ready(str(ws.path), "")
    h = harvest(ws, sb.ticket_path, run_tests=True)
    assert [r.code for r in h.reasons] == ["off_ticket_files"]
    assert len(calls) == 1 and h.verdict == "READY"


def test_H4_commit_bearing_but_two_commits_skips(tmp_path, calls):
    """The case the base still runs roots for: a commit is there, the claim
    resolves, but `commits_ne_1` already settles REWORK."""
    sb, ws = _one(tmp_path)
    _two_commits(ws)
    h = harvest(ws, sb.ticket_path, run_tests=True)
    assert calls == []
    assert h.verdict == "REWORK"
    assert h.facts["tests_run"].startswith("skipped: ")
    first = next(r.code for r in h.reasons if r.blocking)
    assert h.facts["tests_run"] == f"skipped: {first}"


def test_H5_no_test_file_skips(tmp_path, calls):
    sb, ws = _one(tmp_path)
    work_no_test(str(ws.path), "")
    h = harvest(ws, sb.ticket_path, run_tests=True)
    assert calls == []
    assert h.facts["tests_run"] == "skipped: no_test_file"


def test_H6_skip_keeps_the_same_blocking_reasons(tmp_path, calls):
    sb, ws = _one(tmp_path)
    _two_commits(ws)
    off = harvest(ws, sb.ticket_path, run_tests=False)
    on = harvest(ws, sb.ticket_path, run_tests=True)
    blocking = lambda h: [r.code for r in h.reasons if r.blocking]  # noqa: E731
    assert blocking(on) == blocking(off)
    assert on.verdict == off.verdict == "REWORK"


def test_H7_lock_held_only_around_the_roots(tmp_path, monkeypatch):
    lock = _Recording()
    held_inside: list = []

    def fake(cwd, budget_sec=0.0, **_kw):
        held_inside.append(lock.held)
        return GREEN, []

    monkeypatch.setattr(harvest_mod, "run_tests_detail", fake)
    sb, ws = _one(tmp_path)
    work_ready(str(ws.path), "")
    h = harvest(ws, sb.ticket_path, run_tests=True, test_lock=lock)
    assert h.verdict == "READY"
    assert lock.log == ["enter", "exit"]
    assert held_inside == [True]


def test_H8_lock_never_entered_when_skipped(tmp_path, calls):
    lock = _Recording()
    sb, ws = _one(tmp_path)
    _two_commits(ws)
    harvest(ws, sb.ticket_path, run_tests=True, test_lock=lock)
    assert lock.log == []


def test_H9_lock_released_when_the_roots_raise(tmp_path, monkeypatch):
    lock = _Recording()

    def boom(cwd, budget_sec=0.0, **_kw):
        raise RuntimeError("roots blew up")

    monkeypatch.setattr(harvest_mod, "run_tests_detail", boom)
    sb, ws = _one(tmp_path)
    work_ready(str(ws.path), "")
    with pytest.raises(RuntimeError):
        harvest(ws, sb.ticket_path, run_tests=True, test_lock=lock)
    assert lock.log == ["enter", "exit"]


# ── M: the rework prompt ────────────────────────────────────────────────────

def test_M1_skipped_harvest_says_not_run(tmp_path, calls):
    sb, ws = _one(tmp_path)
    _two_commits(ws)
    h = harvest(ws, sb.ticket_path, run_tests=True)
    msg = rework_message(h, 1, 3)
    assert msg.count(NOT_RUN) == 1
    bullets = [i for i, ln in enumerate(msg.splitlines()) if ln.startswith("- ")]
    assert msg.splitlines().index(NOT_RUN) > bullets[0]


def test_M2_ran_harvest_text_unchanged():
    reasons = (Reason("tests_failed", "the tests do not pass: tests ✗ 1"),)
    h = Harvest(verdict="REWORK", reasons=reasons, commit="a" * 40,
                facts={"tests_run": "tests ✗ 1"}, elapsed=0.1)
    msg = rework_message(h, 1, 3)
    assert NOT_RUN not in msg
    assert msg == rework_message(dataclasses.replace(h, facts={}), 1, 3)


# ── R: the runner ───────────────────────────────────────────────────────────

def _race(tmp_path, monkeypatch, slots):
    """agent-b READY, its roots block on an event; agent-a at the base with no
    commit. Returns how long agent-a's harvest took and its verdict."""
    sb = Sandbox(tmp_path, ["agent-a", "agent-b"])
    cfg = make_config(["agent-a", "agent-b"], agent_suite_slots=slots)
    runner_mod._SUITE_SLOTS.configure(runner_mod._suite_slots_armed(cfg))
    work_ready(str(sb.ws("agent-b").path), "")
    inside, release = threading.Event(), threading.Event()

    def blocking(cwd, budget_sec=0.0, **_kw):
        inside.set()
        release.wait(60)
        return GREEN, []

    monkeypatch.setattr(harvest_mod, "run_tests_detail", blocking)
    out: dict = {}
    tb = threading.Thread(target=lambda: out.__setitem__(
        "b", runner_mod._harvest(sb.ws("agent-b"), sb.ticket_path, True, cfg)), daemon=True)
    tb.start()
    try:
        assert inside.wait(30), "agent-b never reached its roots"
        ta = threading.Thread(target=lambda: out.__setitem__(
            "a", runner_mod._harvest(sb.ws("agent-a"), sb.ticket_path, True, cfg)), daemon=True)
        t0 = time.monotonic()
        ta.start()
        ta.join(15)
        took = time.monotonic() - t0
        a_done = not ta.is_alive()
    finally:
        release.set()
        tb.join(30)
    return a_done, took, out


def test_R1_mechanical_harvest_does_not_wait_slots_armed(tmp_path, monkeypatch):
    a_done, took, out = _race(tmp_path, monkeypatch, slots=1)
    assert a_done, f"agent-a still waiting after {took:.1f}s behind agent-b's roots"
    assert out["a"].verdict == "REWORK"
    assert out["a"].facts["tests_run"].startswith("skipped: ")
    assert out["b"].verdict == "READY"


def test_R2_mechanical_harvest_does_not_wait_slots_off(tmp_path, monkeypatch):
    a_done, took, out = _race(tmp_path, monkeypatch, slots=0)
    assert a_done, f"agent-a still waiting after {took:.1f}s behind agent-b's roots"
    assert out["a"].verdict == "REWORK"


def test_R3_a_queued_roots_run_still_reports_wait_and_ahead(tmp_path, monkeypatch):
    """KC-57 kept: two READY agents; the second waits for the first's roots and
    its verdict carries the wait and one ahead. The lock is empty afterwards."""
    sb = Sandbox(tmp_path, ["agent-a", "agent-b"])
    cfg = make_config(["agent-a", "agent-b"], agent_suite_slots=0)
    runner_mod._SUITE_SLOTS.configure(0)
    for n in ("agent-a", "agent-b"):
        work_ready(str(sb.ws(n).path), "")
    inside, release = threading.Event(), threading.Event()

    def roots(cwd, budget_sec=0.0, **_kw):
        if not inside.is_set():         # agent-b, first in: holds the roots
            inside.set()
            release.wait(60)
        return GREEN, []

    monkeypatch.setattr(harvest_mod, "run_tests_detail", roots)
    out: dict = {}
    tb = threading.Thread(target=lambda: out.__setitem__(
        "b", runner_mod._harvest(sb.ws("agent-b"), sb.ticket_path, True, cfg)), daemon=True)
    tb.start()
    assert inside.wait(30)
    ta = threading.Thread(target=lambda: out.__setitem__(
        "a", runner_mod._harvest(sb.ws("agent-a"), sb.ticket_path, True, cfg)), daemon=True)
    ta.start()
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        st = runner_mod._TEST_RUNS_LOCK.status("agent-a")
        if st and st[0] == "queued":
            break
        time.sleep(0.05)
    time.sleep(1.2)
    release.set()
    tb.join(30)
    ta.join(30)
    assert out["a"].verdict == out["b"].verdict == "READY"
    assert out["a"].waited >= 1.0, out["a"].waited
    assert out["a"].ahead == 1, out["a"].ahead
    assert out["b"].ahead == 0
    for n in ("agent-a", "agent-b"):
        assert runner_mod._TEST_RUNS_LOCK.status(n) is None
