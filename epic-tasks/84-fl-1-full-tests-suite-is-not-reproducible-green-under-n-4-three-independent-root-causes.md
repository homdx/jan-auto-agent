# FL-1 — `pytest tests -n 4` is not reproducible green: three independent root causes, one of them a real lock-across-`fsync` hazard, one of them a `MagicMock` masquerading as a 1-second budget

**Status:** next — KC-34 (73) landed `3bc10b8`; this runs before the new epic; found 2026-09-21 while finishing KC-25. `python3 -m pytest tests -n 4 -q --timeout=180` run in a loop: **19 runs, 8 red (42 %), 11 green.** Every red run lost between one and four tests, and **different tests each time** — that is the fingerprint of independent races, not one flaky worker. Reconfirmed 2026-09-21 by a second pass of the same loop (`12 runs, 3 red`).

**Severity:** HIGH for family A (a production lock spans a blocking disk sync); MEDIUM for family B (a truthy non-numeric silently installs a 1-second wall-clock budget); MEDIUM for family C (the `-n 4` suite is not green, so a green full run is not evidence).
**File:** family A `tools/auto/auto_metrics.py` (`record_gate2`) + `tools/metrics_collector.py` (`record`); family B `tools/auto/outer_loop.py` (`_task_budget_seconds`, `_run_rounds`); family C `tests/test_contest_runner.py`.
**Symbol:** family A `AutoMetricsStream.record_gate2`, `AutoMetricsStream._lock`, `MetricsCollector.record`; family B `OuterLoop._task_budget_seconds`, `OuterLoop._run_rounds`, `_TaskBudget`; family C `test_a_silent_agent_stalls_next_to_a_chatty_one`, `test_idle_event_timeout_stalls_a_silent_session`, `test_a_stalled_turn_with_a_valid_commit_is_harvested_to_ready`, `test_three_questions_in_one_turn_stall_and_abort`, `_BenchFake.pulse`.
**Round:** 84
**Size:** M
**Source:** 19 back-to-back runs of `python3 -m pytest tests -n 4 -q --timeout=180` on `73c8d9c`, Python 3.12.3, `pytest-timeout` 180 s. 8 red, distinct failures: `TestThreadSafety::test_concurrent_record_gate2_consistent_total` ×4, `test_a_silent_agent_stalls_next_to_a_chatty_one` ×3, `TestRewriteGateContextSatisfied::test_rewriter_called_when_context_satisfied` ×3, `test_three_questions_in_one_turn_stall_and_abort` ×1, `test_idle_event_timeout_stalls_a_silent_session` ×1, `test_a_stalled_turn_with_a_valid_commit_is_harvested_to_ready` ×1. The `pytest-timeout` stack dump from a hung family-A run is the single most informative artifact: all 20 workers parked at `tools/auto/auto_metrics.py:179 with self._lock:`, and the one holder at `tools/metrics_collector.py:83 os.fsync(f.fileno())`.
**Depends on:** ordering only — KC-34 (73) lands first. Overlaps KC-38 (77) on `TestThreadSafety` and the `test_contest_runner.py` bounds: the two must not run in the same round, and whichever lands second re-reads the other's diff. The three families touch disjoint modules and can land as three independent rounds; only the file list below must not cross.
**Also touches:** `tests/test_auto_e2.py`, `tests/test_coder_smart_context.py`, `tests/test_contest_runner.py`.

---

## What happens today

Three unrelated things make the suite red, and none of them is the same bug.

**A — the metrics lock spans a blocking `fsync`, and 1000 serialized `fsync`s blow the 180 s timeout.**
`AutoMetricsStream.record_gate2` takes `self._lock` for the whole read-modify-write (`tools/auto/auto_metrics.py:179`), and inside that critical section `MetricsCollector.record` does `mkstemp` + `json.dump(records, indent=2)` + `f.flush()` + **`os.fsync(f.fileno())`** + `os.replace` (`tools/metrics_collector.py:67–92`). `fsync` is a *disk* barrier: it is not microseconds, it is a round trip to the storage layer.

`TestThreadSafety::test_concurrent_record_gate2_consistent_total` is 20 threads × 50 records = **1000 records, each one of those full rewrite-and-fsync cycles, all behind one `threading.Lock`**. Under `-n 4` — four workers, each running its own pytest process with its own I/O — the queue does not drain inside 180 s and `pytest-timeout` kills the run. The stack dump from the kill is unambiguous and is quoted in full in *Diagnosis* below: 19 workers waiting to *acquire*, 1 worker holding the lock *inside `fsync`*. Nobody is deadlocked; the lock is doing exactly what it was told, and it was told to cover a syscall that is allowed to block.

This is a production hazard, not only a test hazard. In a real autonomous run every Gate-2 verdict is a `record_gate2` call, and the docstring at `auto_metrics.py:56` says the class is thread-safe — it is, by holding a process-wide lock across a blocking disk sync. So a single slow `fsync` serializes every other metrics writer behind it, and a run that records metrics from more than one thread pays a full disk round trip per record with no overlap at all. The `threading.Lock` was added (per the test docstring) to stop an *unprotected* read-modify-write from dropping >95 % of writes; the cure was traded for a livelock-ish stall under load.

**B — `float(MagicMock())` is `1.0`, so a fake inner loop installs a 1-second task budget that the test never asked for.**
`OuterLoop._task_budget_seconds` (`tools/auto/outer_loop.py:654–663`) is:

```python
return float(getattr(self.inner_loop, "max_task_seconds", 0) or 0)
```

The comment above it promises that "a malformed `max_task_seconds` must mean 'no guard', not an exception at the resume point". It handles `TypeError`/`ValueError` from `float(...)`. It does not handle the case that `float(...)` *succeeds* on the wrong thing. `unittest.mock.MagicMock` implements `__float__`, and `float(MagicMock())` is `1.0` (verified: `float(MagicMock()) == 1.0`, `float(True) == 1.0`, `float('3') == 3.0`).

`TestRewriteGateContextSatisfied._make_outer_loop` builds `inner_loop=MagicMock()` and never sets `max_task_seconds`. `getattr(mock, "max_task_seconds", 0)` therefore returns *another* MagicMock, `MagicMock or 0` keeps it because it is truthy, and `float()` turns it into **`1.0`**. The test that is being run is a behaviour test about whether `TaskRewriter.rewrite` is called — it has no business owning a wall-clock budget. But now `OuterLoop` has one: 1 second across all rounds.

That budget is what kills it. The test needs the rewriter to be reached, and the fixture sets `rewrite_every_n_rounds=2` with `max_rounds=5` — so the rewriter is only eligible from round 2. `_run_rounds` checks the deadline **before starting another round** (`outer_loop.py:358–363`), and on a loaded box round 1 alone consumes more than 1 second. The loop stops before round 2, `rewriter.calls` is 0, and the assertion fails with `assert 0 > 0`. The captured log names the mechanism exactly:

```
WARNING tools.auto.outer_loop:outer_loop.py:362 OuterLoop: task SCTX-OUTER-1
wall-clock budget (1s = 0.0 min) exhausted across rounds — stopping before round 2.
```

`1s = 0.0 min` in that line is the tell. A real budget is configured in minutes; `1 s` is what `float()` made of a MagicMock. Same defect for `max_task_seconds=True` (a `bool` is a truthy non-numeric, and `float(True) is 1.0`) or any string that happens to parse.

**C — wall-clock upper bounds on top of a 1-second silence window.**
This is the family the second pass of the loop found too, and the one most likely to be misread as load. `tests/test_contest_runner.py` stalls a session with `idle_event_timeout_sec=1` and then asserts an *upper bound on real elapsed time* — `assert time.monotonic() - started < 5`, `< 10`, `< 4.0`, and `assert elapsed < cfg.idle_event_timeout_sec + 10`. Those bounds assume a 1-second wall-clock window plus a few seconds of slack, and a loaded box does not honour that. Observed:

- `test_idle_event_timeout_stalls_a_silent_session` — `assert 12.520298890998674 < (1 + 10)`; bound 11 s, measured 12.5 s.
- `test_a_stalled_turn_with_a_valid_commit_is_harvested_to_ready` — `assert (True and (29934.78916157 - 29925.612427754) < 5)`; bound 5 s, measured 9.18 s.
- `test_three_questions_in_one_turn_stall_and_abort` — the same shape at bound 10 s.
- `test_a_stalled_turn_with_a_rejected_commit_stays_stalled_without_a_reprompt` — `run.commit is None` while `last_error == 'no event for 1s'`, i.e. the stall detector fired while the commit the test made under `_run_one` had not yet been observed.

`test_a_silent_agent_stalls_next_to_a_chatty_one` is the sharpest one and it is **not** a timing bound at all; it is a semantic race inside the test's own premise. It wants one agent to go silent and one to stay chatty, and asserts that *exactly one* `/abort` was sent:

```python
(abort_at, _), = [s for s in stamps if s[1].endswith("/abort")]
```

On a loaded run more than one abort is sent and the assertion dies as `ValueError: too many values to unpack (expected 1)`. Two more runs of the same test instead failed `assert run.state is AgentState.READY` for the *chatty* agent with `(AgentState.STALLED, 'no event for 1s')`. Both point at `_BenchFake.pulse`:

```python
def pulse(self, session_id, every, times, then_idle=False):
    def run():
        for _ in range(times):
            time.sleep(every)          # 0.3 s, wall clock
            self._emit({...})
```

The chatty agent's heartbeat is `time.sleep(0.3)` in a daemon thread, and the runner's silence window for it is **1 s**. The margin between the two is 0.7 s of *wall* clock, which a daemon thread on a loaded box does not guarantee — the scheduler is free to delay that `sleep` return by more than 0.7 s. When it does, the agent that the test believes is chatty crosses the 1 s silence threshold, gets stalled, and gets aborted. The test then fails either on the abort count or on the chatty agent's state, depending on which assertion it reaches first. The behaviour under test (`wait_idle` counting only its own session's events, KC-12) is doing the right thing; the fixture's heartbeat is the thing that is wrong.

## Diagnosis — how to reproduce and how to tell the three apart

All three are load-sensitive, so they only show up with the real worker count. Reproducing with `-n 1` or a single test will not find them, and a single green full run is not evidence of anything:

```bash
# the loop that found all three families; 8 red out of 19 on 73c8d9c
for i in $(seq 1 20); do
  python3 -m pytest tests -n 4 -q --timeout=180 --tb=long -rf > /tmp/fl_$i.log 2>&1
  grep -E "^FAILED" /tmp/fl_$i.log && echo "--- red run $i"
done
grep -h "^FAILED" /tmp/fl_*.log | sort | uniq -c | sort -rn
```

Do not use `-q` alone — the `-rf` short summary is what tells you *which* test lost, and the traceback is what tells you which family:

- `pytest-timeout` kills the run and prints a **stack dump of every thread**. If most workers are parked at `tools/auto/auto_metrics.py:179 with self._lock:` and exactly one frame is at `tools/metrics_collector.py:83 os.fsync(f.fileno())`, that is family A. The lock holder is inside the syscall; nobody is spinning.
- `assert 0 > 0` from `TestRewriteGateContextSatisfied`, and `OuterLoop: task … wall-clock budget (1s = 0.0 min) exhausted across rounds — stopping before round 2` in the captured log: family B. The `1s = 0.0 min` in that warning line is the signature — no real budget is 1 second, and the fixture never set one.
- `AssertionError` on a `time.monotonic() - started < N` bound, or `ValueError: too many values to unpack` from the single-abort unpack in `test_a_silent_agent_stalls_next_to_a_chatty_one`: family C.

Isolating one family for a bisect or a patch:

```bash
# family A — the whole class, one test; hangs under load, dies at --timeout
python3 -m pytest tests/test_auto_e2.py::TestThreadSafety -n 4 -q --timeout=180 --tb=long

# family B — needs the wall-clock pressure to make round 1 exceed 1 s
python3 -m pytest tests/test_coder_smart_context.py::TestRewriteGateContextSatisfied \
    -n 4 -q --timeout=180 -rf

# family C — the runner's stall tests, all of them bound on real elapsed time
python3 -m pytest tests/test_contest_runner.py -n 4 -q --timeout=180 -rf -k "stall or silent"
```

Two things worth checking by hand before touching code, because they settle the shape of the fix:

```bash
# family A: how expensive is one record, and is the lock the reason it serializes?
python3 - <<'PY'
import threading, time, pathlib
from tools.auto.auto_metrics import AutoMetricsStream
d = pathlib.Path("/tmp/fl1-a"); (d/"x").mkdir(parents=True, exist_ok=True)
s = AutoMetricsStream(d/"x")
t0 = time.monotonic()
for i in range(1000):                      # serial, single thread
    s.record_gate2(f"T{i}", approved=True, feedback="x")
print("serial 1000 records:", round(time.monotonic()-t0, 2), "s")
PY
# if that alone is a substantial fraction of 180 s, the fsync is the cost and the
# lock is only what prevents 20 threads from overlapping it.

# family B: the coercion the guard misses
python3 -c "from unittest.mock import MagicMock; print(float(MagicMock()))"   # 1.0
```

## What must change

**A — `tools/auto/auto_metrics.py`, `tools/metrics_collector.py`**
Stop holding `AutoMetricsStream._lock` across the blocking write. Two acceptable shapes, either one:

1. `MetricsCollector` owns its own write lock and `record()` takes it around the `mkstemp`/`json.dump`/`fsync`/`os.replace` sequence, while `AutoMetricsStream._lock` covers only the in-memory read-modify-write (load, append, hand the record over). Two locks, each held for a short critical section, no lock held across a disk barrier. The collector's own `fsync` stays — that is the crash-safety fix it was added as — it just stops being serialized behind a *caller's* lock.
2. Decouple durability from the call path: `record()` writes the file and makes the append durable enough for the run, and `fsync` happens at `AutoMetricsStream.flush()` (the existing shutdown hook, `auto_metrics.py:192`) or on a bounded cadence rather than once per record. This keeps the per-record cost to a `os.replace` on the same filesystem, which is what makes the 1000-record loop cheap.

Either way `record_gate2` must keep "Never raises" (`auto_metrics.py:167`) — the lock is a correctness contract for the caller, not an excuse to block on storage. Whatever lands, `_warn_if_contaminated` and `collector` must still be callable from another thread without holding `_lock`.

`TestThreadSafety::test_concurrent_record_gate2_consistent_total` should then complete far inside 180 s at 20×50, and it should keep asserting the count — that assertion is the one that justified the lock in the first place. It must not be weakened, and `--timeout` must not be raised to make it pass: the point of the test is that 1000 concurrent records produce 1000 records, and it may still fail if a real regression drops writes again.

**B — `tools/auto/outer_loop.py`**
Make the budget guard refuse a value it cannot interpret as seconds, instead of coercing it. Concretely, in `_task_budget_seconds`: only accept a real number (`isinstance(v, (int, float))` **and not** `bool`) greater than zero, and return `0.0` for everything else — a `MagicMock`, `True`, `'3'`, an enum, a `None` that slipped past `getattr`. The existing `try/except (TypeError, ValueError)` keeps its job of not throwing at the resume point; it just needs to cover the values that `float()` accepts happily. Then `_run_rounds`'s deadline check is unreachable for a task that has no budget, which is the contract the comment already promises.

`TestRewriteGateContextSatisfied._make_outer_loop` should be fixed too and not just rely on the guard: set `mock_inner.max_task_seconds = 0` explicitly so the fixture states what it means, and give `max_rounds` enough room that the round the rewriter is eligible for is reachable without depending on a 1-second budget. A test that asserts a rewriter is called must not be spending its wall clock to get there.

**C — `tests/test_contest_runner.py`**
Remove the wall-clock upper bounds and assert on the *classification* the runner produced, which is what the tests are actually about. `run.state is AgentState.STALLED`, `turn["idle_status"] == "stalled"`, and `run.last_error == "no event for 1s"` already say the idle-event path was taken and not the 30-second `turn_timeout` path — those assertions are load-independent and prove the same thing the `elapsed < N` bounds were proxying. Drop the `< 5`, `< 10`, `< 4.0`, and `< idle_event_timeout_sec + 10` comparisons, or if a bound is genuinely needed to keep the two paths from blurring, derive the margin from the configured window rather than a hand-written constant and make it large enough to survive a loaded box.

`_BenchFake.pulse` needs the chatty agent's heartbeat to stop depending on `time.sleep(0.3)` beating a 1 s silence window. Two shapes: widen the margin so the heartbeat cannot plausibly drift past the window (an interval that is a small fraction of the window, or the window left at its default and the interval dropped), or drive the heartbeat from the scenario script — emit the `session.status busy` events synchronously as part of `_run_turn` rather than from a competing daemon thread whose `sleep` return the scheduler can delay by more than 0.7 s. If `pulse` keeps a thread, its interval must stay comfortably under the window that is configured for the *other* agent, and the test should assert on the chatty agent's state explicitly instead of letting `AgentState.STALLED` on the chatty side surface as `too many values to unpack`.

`test_a_stalled_turn_with_a_rejected_commit_stays_stalled_without_a_reprompt` (`run.commit is None` while the stall text is present) needs the commit observation to stop racing the stall detector: whatever the runner reads the branch state from should be read at the point the stall is declared, so the test's commit is visible when the claim is scored. If the harness already commits before the stall window opens, the fix is in the ordering of the stall declaration relative to the worktree read, not in the test.

## Acceptance

- [ ] `python3 -m pytest tests -n 4 -q --timeout=180` in a 20-run loop is green in every run, and `grep -h "^FAILED" /tmp/fl_*.log | sort | uniq -c` is empty. This is the only acceptance that covers all three families at once, so it must be the loop and not a single run.
- [ ] Family A: `TestThreadSafety::test_concurrent_record_gate2_consistent_total` completes at 20 threads × 50 records inside 180 s with the **record count assertion unchanged**, and no `--timeout` value anywhere was raised to make it pass. `AutoMetricsStream._lock` is not held across `MetricsCollector.record`'s `fsync` — verifiable by reading `auto_metrics.py:179` and `metrics_collector.py:67–92` and by the timed single-thread loop in *Diagnosis* coming back far below the timeout. `record_gate2` still never raises.
- [ ] Family B: `_task_budget_seconds` returns `0.0` for `MagicMock()`, `True`, `'3'`, `0`, `None` and for an inner loop that has no such attribute at all, and returns the real value for a real number. `TestRewriteGateContextSatisfied::test_rewriter_called_when_context_satisfied` passes under `-n 4` and its fixture sets the budget explicitly; the `wall-clock budget (1s = 0.0 min) exhausted across rounds` line must not appear in its captured log.
- [ ] Family C: no `time.monotonic() - started <` bound remains in `tests/test_contest_runner.py` on a stall assertion, the tests assert on `run.state` / `turn["idle_status"]` / `run.last_error` instead, and `test_a_silent_agent_stalls_next_to_a_chatty_one` no longer unpacks a single abort from a list that can hold more than one. `_BenchFake.pulse` cannot drift past the silence window configured for the other agent.
- [ ] `pytest tests -n 4 -q --timeout=180` and `pytest tests_bugfix -n 4 -q --timeout=180` both green, run sequentially.
- [ ] `python3 scripts/sync_test_tiers.py --check` clean; no new test file left untiered.

## Out of scope

- Widening `--timeout=180` or adding per-test timeouts to make anything pass. That hides all three families; the timeout is doing useful work here, it is what produced the stack dump that identified family A.
- Weakening `TestThreadSafety`'s count assertion, or `TestRewriteGateContextSatisfied`'s `rewriter.calls > 0`. Both are the original defect guards; they earn their keep by failing.
- Changing `idle_event_timeout_sec` defaults, `turn_timeout_sec`, or the runner's stall semantics. The runner's behaviour is correct; the fixtures and the assertions around it are the problem.
- The `PytestRemovedIn10Warning` about class-scoped fixtures defined as instance methods on `TestThreadSafety` and the other `test_auto_g*` classes. It is a real migration item but it is not flakiness, and it does not appear in any of the red runs.
- `tools/auto/collect_bridge.py`. `CollectBridge._shrink` is byte-identical and stays that way.

## Self-check before `append_task.py` (required — every item, in the worktree you submit)

- [ ] `python3 --version`; no backslash and no nested same-quote inside an f-string expression.
- [ ] Exactly one commit on top of the base; only this ticket's work.
- [ ] `git diff --stat <base>..HEAD` names only `tools/auto/auto_metrics.py`,
      `tools/metrics_collector.py`, `tools/auto/outer_loop.py`,
      `tests/test_contest_runner.py`, `tests/test_auto_e2.py`,
      `tests/test_coder_smart_context.py` — plus `.smoke_tests/` links if a new
      test file is added. Never `epic-tasks/`.
- [ ] The 20-run loop from *Diagnosis* is green in every run, and the command is
      quoted in the commit note so the next round can repeat it.
- [ ] No `--timeout` value was raised, and no assertion that catches a real
      regression was weakened.
- [ ] `python3 -m pytest tests -n 4 -q --timeout=180` then
      `python3 -m pytest tests_bugfix -n 4 -q --timeout=180`, **sequentially**,
      both green.
- [ ] `CollectBridge._shrink` byte-identical.
- [ ] `scripts/append_task.py` from the worktree with the **sha** of the one
      commit — not `HEAD`; hand in `git format-patch <base>..HEAD`.

## Ground rules (same as every round)

- Do not touch `CollectBridge._shrink`.
- No test starts a real `kilo` or calls a live provider.
- Do not edit `epic-tasks/`.
- One commit, no push; a test ships with the change and fails without it.
