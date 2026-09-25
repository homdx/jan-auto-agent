# KC-58 — an agent's own full-suite run waits for a round-wide slot, and a turn that is waiting for its tests is not a turn that stopped

**Status:** queued — found 2026-09-25 reading round 103 (FL-9, `/mnt-fs/jan-auto-agent/contest-out/103/`): four of ten agents `STALLED` at the turn deadline. Each had finished its code 30–45 minutes earlier and spent the rest of its hour waiting for its own `pytest tests -n 4`, which took 20–25 minutes because every other agent was running one too.
**Severity:** HIGH. Four finished entries were lost in one round. They had 131–327 lines of work, uncommitted and not harvested. Rebuilt from their worktrees, they are judged in round 103. The ticket tells every agent to run both roots before handing in, so every round has this shape.
**File:** `tools/contest/runner.py`, `tools/contest/policy.py`
**Symbol:** `run_agent` (`on_permission`), `_turn_deadline` (`on_deadline`), `_TEST_RUNS_LOCK`
**Round:** 105
**Size:** M
**Source:** `contest-out/103/state.json` and `<agent>/events.jsonl` (tool parts `completed`: `time.start`/`time.end`):

| agent | last edit | its own full-suite runs | ended |
|---|---|---|---|
| hy3 | +18.9 m | `tests` +25.5 → +45.5 m (20.0 m), `tests_bugfix` +46.0 → +64.8 m (18.8 m), then `sleep 300 && tail …` | `STALLED` +70.3 m, "unchanged for 10m" |
| sensenova-6-8-flash-lite-var1 | +29.9 m | `tests` +32.2 → +57.7 m (25.5 m), `tests_bugfix` +61.3 → +70.3 m (9.0 m) | `STALLED` +70.3 m |
| sensenova-6-8-flash-lite-var2 | +62.3 m | `tests` +29.0 → +49.0 m (20.0 m), then `sleep 600`, `sleep 420` polling a background run | `STALLED` +80.0 m |
| sensenova-6-7-flash-lite-var2 | +59.8 m | `tests` +26.1 → +51.4 m (25.3 m), a second one in the background, killed by itself at +69.6 m | `STALLED` +70.3 m |

On this box `tests -n 4` takes 4–5 minutes alone (the judge's runs of the
same patches, sequentially). With every agent that has finished its code
running its own `-n 4` at once, and the harvest's own suite under
`_TEST_RUNS_LOCK` next to them, each run takes 20–25 minutes.

Nothing coordinates this. `policy.decide` approves `python3 -m pytest tests -n 4`
at once, because it is a mechanical allow with no path outside the worktree.
`_TEST_RUNS_LOCK` serializes only the runner's own harvest, not the agents'
runs.

The deadline makes it worse. KC-36 extends a turn only when the worktree's
`(files, lines)` grew since the last deadline. An agent waiting for its suite
changes nothing, so the first extension (60 → 70 min) is the last:
"no idle after 70m (2 files, 652 lines, unchanged for 10m)". KC-47 keeps the
*silence* clock off a running `bash`. The *turn* deadline has no such rule.

**Depends on:** KC-36 (`_turn_deadline`), KC-47 (a running `bash` is not silence).
**Also touches:** KC-41 (a deadline commit and harvest for the uncommitted work — the safety net; this ticket is the cause), KC-57 (the harvest's own budget and queue).

---

## What must change

### 1. A round-wide slot for an agent's full-suite run

A `bash` permission whose command runs a whole pytest root is held until a
slot is free. "Whole root" is decided by the shape of the command, never by a
list of this repo's directory names: a `pytest` (or `python -m pytest`)
invocation whose path arguments are all directories, or that has no path
argument at all. A `.py` file, a `::node` or a `-k`/`-m` selector means a
targeted run, and it is answered at once. So `tests`, `tests_bugfix`,
`.smoke_tests` and a future `.smoke_fast` are all whole roots, with nothing to
update when a tier is added or renamed. The parser lives in one function,
next to its tests.

- **Where it waits.** In `on_permission`, the agent's own `wait_idle` loop, which has nothing else to read while its call waits.
- **Slot count.** `ContestConfig.agent_suite_slots`, default 1; `0` means off, byte for byte today's behaviour. Waiters are served FIFO.
- **The runner's harvest counts as holding a slot.** Either the harvest takes the same semaphore, or the semaphore wraps `_TEST_RUNS_LOCK`. The round decides. Its wall time is KC-57's `harvest_budget_sec`.
- **Only a delay.** The reply to Kilo is delayed, never refused.
- **Heartbeat.** It shows the waiter as `WAITING (suite queued, 2 ahead)`.

**A slot is held by a process, not by a tool part.** The tool part is the
wrong signal twice over in round 103:
- A suite started in the background (`background_process`, `… &`, `nohup`) completes its part at once while pytest runs on. hy3 and sensenova-6-8-var2 both did this.
- A part cut by Kilo's own `bash` timeout ends while its pytest may run on. agnes-2-0 and agnes-2-5 ran `pytest tests` a dozen times, each part ending after 2.0–2.5 min, well short of the 4–5 min a full run takes.

So, after the reply, the slot stays held while any `pytest` process runs in
the agent's worktree: `/proc/<pid>/cwd` under it, as KC-48's reap scans.
The slot is released when none is left, polled every few seconds, and
always when the agent ends (KC-48's reap runs then anyway).

**A slot has a ceiling: `agent_suite_max_sec`, default 900.** Past it, the
holder's slot is released for the next waiter. The holder's pytest is not
killed: the agent is still running, and KC-48 leaves live agents' processes
alone. The heartbeat names the holder as over its ceiling.

This is the answer to a slow suite like round 92's `agnes-2-5-flash`: its own
tests took 33 min on an idle box. It holds a slot for at most 15 minutes,
then everyone else goes on, while it keeps running beside them as it would
today. Without the ceiling, one slow entry would make every other agent wait
behind it, which is KC-57's harvest queue moved into the agents' own hour.

### 2. Waiting for its own tests is progress for the turn deadline

At a KC-36 deadline, an agent with a `bash` part `running`, a permission
held for a slot, or a pytest process running in its worktree, is extended exactly as if its churn had grown. The extension
is still bounded by `turn_max_sec`, which stays the hard ceiling.

## Seen again in round 106

`sensenova-6-8-flash-lite-var1` (round 106, KC-59) was working to the last
second, and the turn was still aborted:

- +3524 to +4097 s: it ran `pytest tests/test_contest_runner.py tests/test_contest_cli.py`,
  `pytest tests/ -k contest`, `.smoke_tests/` and then the full `pytest tests/`.
  Each took 2–3 min while 11 other agents ran their own suites.
- At +3600 KC-36 granted one extension at 10 files / 1073 lines. At +4200
  the sample was 10 files / **1072** lines: the agent had removed one line
  while fixing a test. That is not "strictly growing", so no second
  extension was granted. Its last `edit` was at +4196, 4 s before the
  abort.
- The report says `no idle after 70m (10 files, 1072 lines, unchanged for 10m)`.
  "unchanged for 10m" is wrong: `_no_idle_error` measures from
  `clock.last_change_at`, which only moves when an extension is **granted**,
  not when the churn changes. Fix it here too: record the time of the last
  sample that differed from the previous one (either number, up or down),
  and print that as "unchanged for".
- The 1072 lines are only in `../rounds/106-sensenova-6-8-flash-lite-var1`.
  Nothing reached `contest-out/106/` (KC-41).

For this ticket: count an edit (a `file.edited` event or a `tool` part
`edit`/`write` completed during the turn's last `turn_extend_sec`) as
progress at the deadline, in addition to a line count that grows. Churn that
goes down is still work.

## Out of scope

- Making the suite faster (FL-1, KC-38).
- Telling the agent not to run the suite: the ticket asks it to, and the harvest re-runs it anyway.
- The uncommitted work at the deadline (KC-41).

## Acceptance

- [ ] Two fake agents each ask for `python3 -m pytest tests -n 4` with `agent_suite_slots = 1`. The second reply is sent only after the first one's pytest processes are gone from its worktree (the `/proc` scan is patched). No real pytest runs, and the clock is patched.
- [ ] `pytest tests/test_x.py -q`, `pytest -k foo` and `pytest tests/test_x.py::t` are answered at once.
- [ ] `pytest .smoke_fast -n 4`, a directory name that exists nowhere in this repo, is held like `pytest tests -n 4`, and so is a bare `pytest -n 4`.
- [ ] A session that stalls while holding a slot releases it: the next queued agent is answered.
- [ ] A suite started in the background holds the slot while its pytest process runs in the worktree, and releases it when the process exits. The `/proc` scan is patched, no real pytest runs.
- [ ] A holder past `agent_suite_max_sec` stops blocking: the next waiter is answered, and the holder's process is not signalled.
- [ ] `agent_suite_slots = 0`: every reply is immediate, byte for byte as today.
- [ ] A deadline reached with a `bash` part `running` extends the turn with unchanged churn. At `turn_max_sec` it does not.
- [ ] Round 106 `sensenova-6-8-flash-lite-var1`: a sample of 1073 → 1072 lines with an `edit` in the last `turn_extend_sec` is extended, and `unchanged for` counts from the last sample that differed, not from the last grant.
- [ ] `tests` and `tests_bugfix` green, sequentially; `CollectBridge._shrink` byte-identical.
