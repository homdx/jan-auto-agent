# KC-38 — The full suite fails on a different test every run: three load flakes, one of them a 206 s I/O-bound hang

**Status:** landed `e500d40` (2026-09-22) — closed without a round: FL-1 (84) fixed all three families in one commit — the metrics lock no longer spans the fsync (`TestThreadSafety`), the fake's turn waits on `on_prompt` (`tests/_kilo_fake.py`), and the kilo-client test lost its wall-clock lower bounds; follow-ups `708f7d0`, `215fe70`, `b5f9278` (FL-6). The original filing follows.  
**Severity:** MEDIUM  
**File:** `tests/_kilo_fake.py` (`FakeKiloServer._run_turn`), `tests/test_auto_e2.py` (`TestThreadSafety`), `tests/test_contest_kilo_client.py` (`test_events_of_the_session_keep_wait_idle_alive`)  
**Symbol:** `FakeKiloServer._run_turn`, `TestThreadSafety`, `test_events_of_the_session_keep_wait_idle_alive`  
**Round:** 77  
**Size:** M  
**Source:** four full runs of `python3 -m pytest tests -n 4 -q --timeout=180` on Linux / Python 3.12.3, `pytest.ini` addopts `-n auto -q --dist=loadgroup`, 4 workers, real 3 m 26 s / user 4 m 59 s. Victims, in order: `test_contest_runner.py::test_a_stalled_turn_with_a_rejected_commit_stays_stalled_without_a_reprompt`, `test_auto_e2.py::TestThreadSafety::test_concurrent_record_gate2_consistent_total`, then again on the runner. A targeted run of `test_contest_runner.py + test_contest_cli.py` added two more victims from the same family (`test_turn_timeout_stalls_a_session_that_never_idles`, `test_a_stalled_turn_with_a_valid_commit_is_harvested_to_ready`); the same pair of files passed twice, once with and once without the KC-25 change. Measured in isolation: `python3 -m pytest "tests/test_auto_e2.py::TestThreadSafety" -q -n 1` = **3 m 04 s real, 0 m 09 s user** — i.e. ~206 s of wall for 9 s of CPU, already past the 180 s per-test timeout with nothing else running.  
**Depends on:** KC-32 (the pattern to copy: bounds on the behaviour, not on the box). Independent of KC-14/15/17/21/29/30/31 — all of them different files.  
**Also touches:** `tests/test_contest_runner.py` (extend, §2's acceptance), `tools/contest/runner.py` (read-only: the stall edge at `if state is not None`), `tools/auto/auto_metrics.py`, `tools/metrics_collector.py` (read-only: the write path the hang is on), `pytest.ini` (read-only).

---

## What happens today

### Flake 1 — `TestThreadSafety::test_concurrent_record_gate2_consistent_total` is a 206 s hang, not a lost write

The test starts 20 threads, each calling `AutoMetricsStream.record_gate2` 50 times — 1000 records. Every call is a **full rewrite of the whole file** (`json.dump(records, f, indent=2)`) plus `flush()` and **`os.fsync()`** and `os.replace()`, inside one `threading.Lock` (`tools/auto/auto_metrics.py:179` → `tools/metrics_collector.py:57-95`). The file is rewritten 1000 times, each copy larger than the last.

Failure signature, verbatim (`--timeout=180`):

```
>       t.join()
tests/test_auto_e2.py:325:
E           Failed: Timeout (>180.0s) from pytest-timeout.
...
  File ".../tools/auto/auto_metrics.py", line 179, in record_gate2
    with self._lock:
```

Stack of **Thread-20, Thread-19, Thread-18, … all 20 workers** parked at the same `with self._lock:` line. The assertion `assert len(records) == n_threads * records_each` — the one that would prove the thread safety — **is never reached**. The test does not demonstrate a race; it demonstrates that the write path cannot do 1000 fsync'd rewrites in 180 s on a loaded box.

Two things make it worse and neither is in the test:

* `tests/test_auto_e2.py` carries **no `xdist_group` mark** (`test_contest_kilo_client.py:68` and `test_contest_runner.py:54` both do), so with `--dist=loadgroup` the 1000-fsync benchmark is free to land on any of the 4 workers *while* every port-binding test is serialised onto one of them.
* `MetricsCollector.__init__` already documents the cost and what it looks like: *"O(N^2) instead of O(N), which is slow enough under CPU contention (parallel test runners, etc.) to look like a hang/deadlock even though no lock is actually circularly held"* (`tools/metrics_collector.py:30-40`). The mtime-keyed cache there does not help: every `record()` rewrites the file, so the mtime changes every call and the cache is a miss every time.

`test_concurrent_all_improvement_json_ok_none` (5 × 10 records) is in the same class and has the same shape, on a smaller budget.

### Flake 2 — the stall hook races the runner's silence window, and the harvest is skipped

`tools/contest/runner.py`, stall edge (the `if state is not None:` block, ~line 701):

```python
if state in (AgentState.STALLED, AgentState.ERROR):
    above = _commits_above(ws)
    if above:
        verdict = _harvest(ws, ticket_path, run_tests)
        ...
        run.commit = verdict.commit or _head_sha(ws)
```

`tests/_kilo_fake.py:414-426` (`_run_turn`) calls the scenario's `on_prompt` hook **on the fake's own thread** — the runner is blocked in `wait_idle` on a different thread — and emits the turn's events *after* it. The stall tests use `"events": []`, so after the prompt there is **no event at all** until the runner's `idle_event_timeout_sec` (1 s in `_stall_config()`) fires. When the hook's `git commit` is still in flight, `_commits_above(ws)` returns 0, the whole `if above:` block is skipped, and the run finishes STALLED with `run.commit is None` and no `turns.jsonl` harvest entry.

Failure signature:

```
>       assert run.commit == _branch_sha(ws)
E       AssertionError: assert None == '8fdfb76478d0eba02b2...'
E         + where None = AgentRun(..., last_error='no event for 1s', commit=None, ...)
tests/test_contest_runner.py:678: AssertionError
```

`run.state is STALLED` and `last_error == 'no event for 1s'` are both correct — the only thing wrong is that the commit the hook just wrote is invisible at the instant the stall edge fires. Three tests in the family hit it (`test_a_stalled_turn_with_a_rejected_commit_stays_stalled_without_a_reprompt`, `test_a_stalled_turn_with_a_valid_commit_is_harvested_to_ready`, `test_turn_timeout_stalls_a_session_that_never_idles`); all three pass on `-n 1` and on the immediately following run.

The hazard is not only in the test: `idle_event_timeout` fires on silence regardless of whether the session is still busy, so a real model that goes quiet for the last second of the window while its own commit is landing hits the same ordering. It is narrow — a live model emits events while it works — but the fake's `"events": []` is exactly the case the tests exist to prove, and the KC-21/KC-30 guarantee ("a stalled turn with a commit on the branch is harvested, never dropped") is what the run then loses.

### Flake 3 — `test_events_of_the_session_keep_wait_idle_alive` sleeps in real time and races its own deadline

`tests/test_contest_kilo_client.py:400-426`: a heartbeat thread emits `session.status busy` every 0.15 s, 8 beats (1.2 s), while `wait_idle(..., timeout=5.0, idle_event_timeout=0.3)` runs. The assertions are `res.status == "idle"`, `res.elapsed > 0.9`, `elapsed > 0.9`, `not h.fake.recorded_abort_for(...)`, `len(events_of("session.status")) >= 3`.

Failure signature:

```
>       assert res.status == "idle"
E       AssertionError: assert 'timeout' == 'idle'
```

The overall 5.0 s deadline expired before the 8th beat arrived: the heartbeat thread was starved past it. Two of the three bounds (`res.elapsed > 0.9`, `elapsed > 0.9`) are wall-clock bounds on the **box**, not on the behaviour — the same shape KC-32 removed from the ctrl-C tests. Both `test_contest_kilo_client.py` and `test_contest_runner.py` are pinned to one worker by `xdist_group("port_bound_http_servers")`, so every port-binding test queues there; this test is the one in the group that sleeps in real time and asserts on it.

### Evidence this is not the KC-25 commit

* `python3 -m pytest tests/test_contest_runner.py tests/test_contest_cli.py -q` — green with the KC-25 change, and green again on the pristine base with the same two files.
* The same runner stall tests pass with `-n 1` and on the next run of the same files: three different victims across four runs is not a deterministic regression.
* Flake 1's file (`tools/auto/auto_metrics.py`, `tools/metrics_collector.py`) and its test (`tests/test_auto_e2.py`) are untouched by KC-25, and the class alone takes 206 s on a quiet box.
* Flake 2's race is between the fake's turn thread and the runner's `wait_idle`; KC-25 touches `intake` in `cli.py` only, before any session exists.
* Flake 3 is in KC-1's client and KC-12's silence clock; KC-25 adds `providers()` next to them and no timing.

## What must change

1. **`TestThreadSafety`: stop being a benchmark, and name its own failure.** Two parts, both in `tests/test_auto_e2.py`:
   * shrink the workload to what proves the invariant — 1000 full-rewrite + `fsync` + `os.replace` cycles is a durability benchmark, not a thread-safety check. Something like 5 threads × 20 records keeps the interleaving (the lock is what makes the count come out right) at a cost of a few hundred ms; if a larger variant is wanted, put it behind a `slow`-style marker, not in the default gate. Keep the assertion: the total must equal the number of calls, no lost writes.
   * replace `t.join()` with `t.join(timeout=...)` per thread and assert every thread joined, so a hang fails with a message naming the workload instead of a 200-line `Timeout (>180.0s)` dump showing 20 threads on one lock line.
   * Do **not** delete `os.fsync` from `MetricsCollector.record` as a side effect. The fsync is a deliberate crash-window fix, commented as such (`tools/metrics_collector.py:76-86`), and removing it changes durability semantics — that is its own decision, not a test fix. If the benchmark has to stay large, the fsync is what makes it slow and should be its own ticket.
2. **`FakeKiloServer._run_turn`: the turn announces itself after the hook runs**, so a silence stall can only fire once the hook's own work is visible. Emit one `session.status busy` (or the turn's first scripted event) *after* `on_prompt` returns and *before* the turn's `idle`, i.e. move the emit to after the hook. The runner's silence clock then starts at hook completion, `_commits_above(ws)` sees the commit, and the KC-21/KC-30 harvest runs. The stall still fires — the window just starts later — so `test_turn_timeout_stalls_a_session_that_never_idles` and its siblings keep proving the timeout, and `turns.jsonl` keeps the stall turn. Alternative if the fake side feels too invasive: give `_stall_config()` an `idle_event_timeout_sec` that is bounded against the hook's cost with a margin (KC-32's rule), but that only widens the window instead of closing the ordering, so prefer the emit.
3. **`test_events_of_the_session_keep_wait_idle_alive`: derive the bounds from the values it is proving.** Either drive the clock (inject the monotonic source into `wait_idle` and step it) or, cheaper: keep `res.status == "idle"`, `not recorded_abort_for`, and `len(events_of("session.status")) >= 3` — the three behavioural facts — and drop the two `> 0.9` wall-clock lower bounds, setting the overall timeout from the heartbeat arithmetic (`beats × interval + idle_event_timeout + margin`) instead of a bare `5.0`.
4. **Make the flakes visible next time.** One of these, so the class stops returning:
   * a `slow` marker plus `@pytest.mark.slow` on the 1000-record variant, excluded from the default addopts; or
   * `pytest.ini` gets `--timeout-method=signal` / a tighter per-test budget so a 206 s test fails fast and named, instead of `Timeout (>180.0s)` with no test name at the top of the log.
   Whichever is chosen, the default gate stays `tests -n 4 -q --timeout=180`.

## Acceptance

- [ ] `python3 -m pytest tests -n 4 -q --timeout=180` is green **five runs in a row** on the operator's box, then the same five for `tests_bugfix -n 4 -q --timeout=180` (sequentially). One green run is not evidence for this ticket.
- [ ] `TestThreadSafety` completes in well under a tenth of the 180 s budget on `-n 1`, and `python3 -m pytest "tests/test_auto_e2.py::TestThreadSafety" -q -n 1` prints a real wall time under it; both thread-safety tests still assert the lost-write invariant (total == number of calls, `improvement_json_ok is None` for every record).
- [ ] A blocked `record_gate2` fails the test with a message naming the threads and the timeout, not `Failed: Timeout (>180.0s) from pytest-timeout.` with a stack of `with self._lock:` lines.
- [ ] `test_a_stalled_turn_with_a_rejected_commit_stays_stalled_without_a_reprompt` asserts `run.commit == _branch_sha(ws)` **and** `turn["harvest"]["verdict"] == "REWORK"` and keeps asserting `run.state is STALLED`, `last_error == 'no event for 1s'`, `attempt` unchanged, and that no second prompt went to the fake.
- [ ] The same three stall tests pass ten runs in a row of `python3 -m pytest tests/test_contest_runner.py -q -n 1`, and the stall still fires when it should (`test_turn_timeout_stalls_a_session_that_never_idles` still aborts, still reaches the deadline).
- [ ] `test_events_of_the_session_keep_wait_idle_alive` carries no `> 0.9` bound on `elapsed`, still asserts `idle`, no abort, and ≥3 heartbeats, and its timeout is computed from `beats × interval + idle_event_timeout + margin`.
- [ ] `CollectBridge._shrink` byte-identical. `git diff <base>..HEAD --stat` names only the test files and `tests/_kilo_fake.py` (plus `.smoke_tests/` links).
- [ ] `python3 scripts/sync_test_tiers.py --check` clean.

## Out of scope

- Removing or deferring the `os.fsync` in `MetricsCollector.record`. That is a durability trade for the whole auto pipeline, not a test-hygiene fix; if the benchmark has to stay at 1000 records, file it separately with the fsync as the subject.
- `turn_timeout_sec` itself and the round's own budgets (KC-32's out-of-scope, unchanged).
- Re-basing `wait_idle` onto an injected clock. Worth doing — it is the only fix that removes the class — but it is KC-12's public contract and a separate ticket.
- Changing the worker count, `pytest.ini`'s `addopts`, or `--timeout` to hide a slow test. The gate stays as the runbook writes it.
- KC-32's own remaining items, and KC-14/15/17/21/29/30/31 — different files.

## Self-check before `append_task.py` (required — every item, in the worktree you submit)

- [ ] `python3 --version` on the judge is **3.10.12**; `python3 -c "import tools.contest.cli, tools.contest.kilo_client"` from the repo root. No backslash and no nested same-quote inside an f-string expression.
- [ ] Exactly **one** commit on top of the base; only this ticket's work.
- [ ] `git diff --stat <base>..HEAD` names only `tests/_kilo_fake.py`,
      `tests/test_auto_e2.py`, `tests/test_contest_kilo_client.py`,
      `tests/test_contest_runner.py`
      (plus `.smoke_tests/` links). Never `epic-tasks/`.
- [ ] `python3 scripts/sync_test_tiers.py --check` is clean.
- [ ] The stall tests are red without the fake change: revert `tests/_kilo_fake.py`
      only and show `test_a_stalled_turn_with_a_rejected_commit_stays_stalled_without_a_reprompt` failing on `run.commit is None`.
- [ ] `python3 -m pytest tests -n 4 -q --timeout=180` then
      `python3 -m pytest tests_bugfix -n 4 -q --timeout=180`, **five times each**,
      **sequentially**, all green.
- [ ] `CollectBridge._shrink` byte-identical.
- [ ] `scripts/append_task.py` from the worktree with the **sha** of the
      one commit — not `HEAD`; hand in `git format-patch <base>..HEAD`.

## Ground rules (same as every round)

- Do not touch `CollectBridge._shrink`.
- No test starts a real `kilo` or calls a live provider.
- Do not edit `epic-tasks/`.
- One commit, no push; a test ships with the change and fails without it.
