"""Randomised state-machine property test for BugFixLoop.handle_regression.

Invariants asserted after every simulated run:
  I1  total OuterLoop calls per ticket <= MAX_FIX_ATTEMPTS (bounded work)
  I2  no BUG-FIX-* task left pending (todo/in-progress) at the end
  I3  ticket id never grows (no cascade)
  I4  ticket ends in a terminal state, or is fixed
  I5  no unhandled exception escapes handle_regression
"""
import random, sys, tempfile, traceback
from pathlib import Path
from dataclasses import dataclass, field
from unittest.mock import MagicMock
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.auto.bug_fix_loop import BugFixLoop, MAX_FIX_ATTEMPTS
from tools.auto.state import StateStore, STATUS_DONE, STATUS_BLOCKED, STATUS_TODO
from tools.auto.ticket_store import make_ticket_store

@dataclass
class ER:
    passed: bool=False; exit_code: int=4; stdout: str="F"; stderr: str=""
    traceback: str=""; timed_out: bool=False
@dataclass
class OR:
    task_id: str="X"; passed: bool=False; exhausted: bool=False
    rounds_used: int=1; feedback_files: list = field(default_factory=list)
    def knowledge(self): return "k"

class Killed(Exception): pass

def task(tid):
    return {"id": tid, "title": "t", "instruction": "i",
            "target_files": ["a.py"], "acceptance_check": "pytest -q"}

def one_run(seed):
    rnd = random.Random(seed)
    d = Path(tempfile.mkdtemp())
    st = StateStore(d/".agent"); st.initialise("g", d)
    tk = make_ticket_store(d/".agent")
    calls = {"n": 0}

    def outer_behaviour(t, base_dir):
        calls["n"] += 1
        r = rnd.random()
        if r < 0.10: raise Killed("run killed mid-OuterLoop")
        if r < 0.30: return OR(passed=True)
        if r < 0.60: return OR(passed=False, exhausted=True)
        return OR(passed=False, exhausted=False)     # 4c no verdict

    o = MagicMock(); o.run_task.side_effect = outer_behaviour
    no_git = rnd.random() < 0.15
    cos = MagicMock()
    def commit(t_, r_):
        # Faithful to CommitOnSuccess: THREE outcomes, not two.
        r = rnd.random()
        if r < 0.15:
            return None                              # GitError: nothing settled
        if r < 0.30:
            st.set_task_status(t_["id"], STATUS_DONE, commit="")
            return None                              # nothing staged: settled
        st.set_task_status(t_["id"], STATUS_DONE, commit="deadbeefcafe")
        return "deadbeefcafe"
    cos.commit.side_effect = commit

    trig = "AUTO-T1"
    events = []
    for step in range(rnd.randint(3, 25)):
        # 15% of runs exercise no-git mode (commit helper is None)
        bfl = BugFixLoop(o, None if no_git else cos, tk, st)
        # randomly perturb like an operator / crash would
        act = rnd.random()
        if act < 0.10:
            t = tk.get("BUG-AUTO-T1")
            if t: tk.update("BUG-AUTO-T1", status="open"); events.append("reset->open")
        elif act < 0.15:
            t = tk.get("BUG-AUTO-T1")
            if t: tk.delete("BUG-AUTO-T1"); events.append("ticket deleted")
        elif act < 0.20:
            if st.get_task("BUG-FIX-AUTO-T1"):
                st.set_task_status("BUG-FIX-AUTO-T1", "todo"); events.append("task revived")
        elif act < 0.30:
            # Controller._reset_resettable_blocked_tasks, replayed verbatim.
            # Runs at EVERY startup; undoing parking here is what reopened
            # the main-queue bypass on resume.
            from tools.auto.utils import highest_completed_round
            for _t in st.all_tasks():
                if _t["status"] != STATUS_BLOCKED:
                    continue
                if _t["id"].startswith("BUG-FIX-"):
                    continue
                if highest_completed_round(st.task_dir(_t["id"])) >= 10:
                    continue
                st.set_task_status(_t["id"], STATUS_TODO)
            events.append("startup reset")
        try:
            res = bfl.handle_regression(task(trig), ER(), base_dir=d)
            events.append(f"{trig} -> {res.ticket_id}/{res.fix_task_id}")
            trig = res.fix_task_id if rnd.random() < 0.5 else "AUTO-T1"
        except Killed:
            events.append("KILLED mid-fix")
        except Exception:
            return ("I5 unhandled exception", events, traceback.format_exc(), calls["n"], st, tk)

    # ---- invariants ----
    tickets = sorted(p.name for p in (d/".agent"/"tickets").glob("*.json"))
    grants = 1 + sum(1 for e in events if e in ("reset->open", "ticket deleted"))
    if calls["n"] > grants * MAX_FIX_ATTEMPTS:
        return ("I1 OuterLoop calls exceed budget", events,
                f"calls={calls['n']} grants={grants} allowed={grants*MAX_FIX_ATTEMPTS}",
                calls["n"], st, tk)
    if any(n.count("BUG-FIX-") > 0 for n in tickets):
        return ("I3 cascade: ticket id grew", events, tickets, calls["n"], st, tk)
    pending = [t["id"] for t in st.resume_info()["pending"] if t["id"].startswith("BUG-FIX-")]
    if pending:
        return ("I2 fix task left pending", events, pending, calls["n"], st, tk)
    t = tk.get("BUG-AUTO-T1")
    if t and t["status"] == "in-progress":
        # legitimate ONLY while attempts remain — otherwise it is a stuck state
        if int(t.get("fix_attempts", 0) or 0) >= MAX_FIX_ATTEMPTS:
            return ("I4 stuck in-progress with budget spent", events, t, calls["n"], st, tk)
    elif t and t["status"] not in ("fixed", "deferred", "verification-failed", "open"):
        return ("I4 non-terminal end state", events, t["status"], calls["n"], st, tk)
    return None

# Seeds are fixed, so a failure here is reproducible: rerun this file
# directly with a larger N to search further.
#
#   $ python3 tests/test_bug_fix_loop_fuzz.py 5000
#
# Every bug in section 6 of test_bug_fix_id_cascade.py was found this way,
# after five hand-review passes had already signed the code off.  The
# hand-written tests cover the paths someone thought to check; this covers
# the interleavings nobody did — crash mid-fix, operator reset, ticket
# deleted under a running fix.

import pytest


@pytest.mark.parametrize("seed", range(150))
def test_invariants_hold(seed):
    failure = one_run(seed)
    assert failure is None, f"{failure[0]} — detail={failure[2]!r} events={failure[1][-8:]}"


if __name__ == "__main__":
    fails = {}
    N = int(sys.argv[1]) if len(sys.argv) > 1 else 400
    for seed in range(N):
        r = one_run(seed)
        if r:
            fails.setdefault(r[0], (seed, r))
    print(f"ran {N} randomised runs")
    if not fails:
        print("no invariant violations")
    for k, (seed, r) in fails.items():
        print(f"\n=== {k}   (first at seed {seed}) ===")
        print("  detail:", str(r[2])[:300])
        print("  events:", " | ".join(r[1][-8:]))
