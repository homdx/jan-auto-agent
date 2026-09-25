# KC-57 — a harvest has a wall-clock budget, and the round sees who waits for the test lock

**Status:** queued — found 2026-09-25 judging round 92 and reading round 103 (both with `run_tests` on).
**Severity:** MEDIUM (one entry's slow suite holds `_TEST_RUNS_LOCK` for 20+ minutes; every other finished entry waits behind it in `HARVESTING`, and the round's last half hour is a queue nobody can see)
**File:** `tools/contest/runner.py`, `tools/contest/harvest.py`, `tools/contest/gates.py`
**Symbol:** `_harvest`, `_TEST_RUNS_LOCK`, `harvest` (`run_tests` block), `gates.run_tests_detail`
**Round:** 104
**Size:** S
**Source:** `contest-out/92/state.json` (this repo), `/mnt-fs/jan-auto-agent/contest-out/103/state.json`

Round 92 — `agnes-2-5-flash` harvested for **1267.7 s** where every other
entry took 162–262 s. Its own tests are the cause: its `_reap_processes`
calls `time.sleep(REAP_GRACE_SEC)` (5 s) on every terminal state, even with
nothing to reap, so every existing `run_agent` test in
`tests/test_contest_runner.py` got 5 s longer. The judge's local run of the
same patch on `kc` `da0bc50`: `tests -n 4` in 1987 s (33 min), where the
round's other patches take about 250 s.

Round 103 (FL-9) — five harvests, one after another under the lock. `elapsed`
counts only the harvest itself, so the queue has to be added up from
`idle_at` (this is an inference from `state.json`: the runner records no
lock-acquired time):

| agent | idle at | harvest | lock free at (inferred) |
|---|---|---|---|
| agnes-2-5-flash | +26.2 m | 1436 s | +50.1 m (its rework prompt went out then) |
| agnes-2-0-flash | +28.5 m | 1507 s | ≈ +75.2 m |
| agnes-2-5-flash (rework) | +53.9 m | 1003 s | ≈ +91.9 m |
| step-3-7-flash | +66.9 m | 587 s | ≈ +101.7 m |
| sensenova-6-7-flash-lite-var1 | +81.7 m | 510 s | ≈ +110.2 m |

`state.json` was last written at +110 m. So `sensenova-6-7-flash-lite-var1`
spent about 20 minutes queued for a 8.5-minute harvest, and `step-3-7-flash`
about 25. The heartbeat (KC-27) shows `HARVESTING` for the whole time, queue
and run alike.

Nothing bounds a harvest's wall time. `--timeout=180` on the pytest call
behind `gates.run_tests_detail` bounds each *test*, not the suite: a suite
made of many 5-second tests passes it.

**Depends on:** KC-16 (`_TEST_RUNS_LOCK`), KC-27 (heartbeat phases).
**Also touches:** KC-50 (a harvest that is already `REWORK` does not take the lock at all). This ticket is about a harvest that does need the lock.

---

## What must change

### 1. A harvest's pytest roots have a wall-clock budget

`ContestConfig.harvest_budget_sec` (default 900, `0` = off). The roots of one
harvest together run at most that long. Past it, pytest is ended (the process
group — see KC-48 for why not only the child) and the verdict is `REWORK` with
a blocking reason `tests_slow`, whose message names the budget and the
slowest tests from `--durations=10`. The rework prompt says the suite is
too slow, not that tests failed.

### 2. The queue is visible

`turn["harvest"]` gains `"waited"`: the seconds spent waiting for
`_TEST_RUNS_LOCK`, next to `elapsed`. The heartbeat shows `HARVESTING
(queued 12m, 2 ahead)` while waiting and `HARVESTING (tests 3m)` once it
holds the lock. The round's final table shows the total lock wait per agent.

## Out of scope

- Running harvests in parallel. The lock exists because two full suites at
  once on this box are what KC-47's stalls are made of.
- Making slow tests fast. That is the entry's job; this ticket makes the
  runner tell it so.

## Acceptance

- [ ] A fake harvest whose pytest sleeps past a patched `harvest_budget_sec`
      is ended, the verdict is `REWORK` with `tests_slow`, and no pytest
      process of it is left running.
- [ ] `harvest_budget_sec = 0` keeps today's behaviour.
- [ ] Two agents harvesting at once: the second one's `turn["harvest"]["waited"]`
      is at least the first one's run time (patched clock, no real sleep),
      and the first one's `waited` is about 0.
- [ ] The heartbeat line of a queued agent says `queued`.
- [ ] `tests` and `tests_bugfix` green; `CollectBridge._shrink` byte-identical.
