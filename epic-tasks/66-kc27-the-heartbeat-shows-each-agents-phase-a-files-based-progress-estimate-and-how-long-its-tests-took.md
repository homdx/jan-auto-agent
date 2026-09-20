# KC-27 — the heartbeat shows each agent's phase, a files-based progress estimate, and how long its tests took

**Status:** queued — **last in the queue**: after every other open and queued KC ticket (59–64, then 50, 46–49); asked for on 2026-09-20 after round 58, where the operator watched eight `WAITING 14m` lines for half an hour with no way to tell an agent still editing from one inside its self-check `pytest`, and read how long the judge's tests took only afterwards from file mtimes in `contest-out/58/`. Same file as KC-21 and KC-22 (`tools/contest/runner.py`) — lands after both; disjoint from KC-19's retry loop.
**Severity:** LOW (operator comfort; nothing about the verdict changes)
**File:** `tools/contest/runner.py` (`_Heartbeat.line`, `run_agent`'s HARVESTING step)
**Symbol:** `_Heartbeat`, `_worktree_files`, `_progress`
**Round:** 66
**Size:** S
**Source:** `contest-out/58/state.json`: the heartbeat (KC-18) says `mimo-v2-5 WAITING 21m (attempt 0)` and nothing else until `HARVESTING`; the tests' duration is not recorded anywhere — the operator reconstructed "≤ 9 min" from `idle_at` 14:58:29 and `state.json`'s mtime 15:07:23. Meanwhile every agent's worktree is on disk and `git status` there says how far the change has got: mimo had 5 files touched at the end, the three that STALLED had 1–2 uncommitted.
**Depends on:** KC-18 (`_Heartbeat`, landed `9c4db8b`), KC-16 (`run_tests` in the harvest, landed `1304950`).
**Also touches:** `tests/test_contest_runner.py`

---

## What happens today

```python
part = f"{run.agent.name} {run.state.value}"
if not run.terminal:
    part += f" {_age(now - self.since.get(run.agent.name, now))}"
    if run.attempt:
        part += f" (attempt {run.attempt})"
```

The line is the state machine's view only: `WAITING` for the whole turn,
`HARVESTING` for the whole harvest (queueing for `_TEST_RUNS_LOCK` and the
tests look the same), then the terminal state. `Harvest.elapsed` — the wall
time of the harvest, measured inside `harvest()` and so **excluding** the wait
for the lock — is computed and dropped; `turn["harvest"]` keeps only
`verdict` and `reasons`.

## What must change

1. **The tests' time is recorded.** `turn["harvest"]` gains
   `"elapsed": round(verdict.elapsed, 1)` (seconds) next to `verdict` and
   `reasons` — in `turns.jsonl` and `state.json` alike; readers that look
   only at the two old keys are unaffected. The transition after the harvest
   names it: the note becomes `<sha12> (tests 4m)` on READY, and the REWORK
   note starts with `attempt N (tests 4m) — …`; when `run_tests` is off the
   parenthesis is `(harvest 3s)`.

2. **A files count per worktree.** `_worktree_files(ws: Workspace) -> int`
   is the number of distinct paths in the union of
   `git -C <path> --no-optional-locks status --porcelain --untracked-files=all`
   (tracked changes and untracked files — the agent is editing) and
   `git -C <path> diff --name-only <base_sha>..HEAD` (what it already
   committed), minus paths under `.smoke_tests/` (`sync_test_tiers.py`
   links). Read-only and never raises: any git failure is `0`. It is called
   from the heartbeat thread only, once per tick per non-terminal agent —
   `--no-optional-locks` so the tick never takes `index.lock` from under the
   agent's own `git commit`.

3. **A progress estimate.** `_progress(state, files, median, committed) -> int | None`
   (per cent, `None` = no bar):
   - `CREATED`, `PROMPTED` → `0`;
   - `WAITING`, `REWORK` (the agent is working) →
     `min(60, round(60 * files / max(median, 1)))`, where `median` is the
     median of `files` over the agents currently working (floor 1) — an
     agent at or above the pack's median is at 60 %; when its branch already
     carries a commit above `base_sha` (`committed`) → `70`;
   - `HARVESTING` → `80`;
   - `READY` → `100`; `GAVE_UP`, `STALLED`, `ERROR` → `None`.
   The heuristic is stated in the docstring as what it is: a files-count
   against the pack, not a measure of the work — good enough to see the
   field converging and one agent stuck at 1 file for 20 minutes.

4. **The line.** `_Heartbeat.line()` renders per agent
   `<name> <STATE> <age> <bar> <pct>% <files>f[ (attempt N)][ (tests 4m)]`,
   the bar ten cells `[######....]`; terminal agents keep today's short form
   plus `(tests 4m)` when the last turn was harvested. Example:

   ```
   round 66 14m: mimo WAITING 14m [######....] 60% 5f · hy3 WAITING 14m [##........] 20% 1f · glm HARVESTING 2m [########..] 80% 4f · step READY (tests 4m) · agnes ERROR — 3 live
   ```

   `progress_every_sec = 0` still starts nothing; the git calls cost about
   8 × 2 per minute on the busiest round and are spent only when a tick runs.

5. Not in `AgentRun`: the files count and the estimate are the tick's, not
   the round's state — `state.json`'s shape changes only by `elapsed`.

## Acceptance

- [ ] `tests/test_contest_runner.py`:
      - `_worktree_files` on a `tmp_path` sandbox: clean worktree → 0; one
        tracked edit + one untracked file → 2; the edit committed and a
        second file edited → 2 (the committed one counted through
        `base_sha..HEAD`); a `.smoke_tests/x` link → not counted; a path that
        is not a git repo → 0, no exception;
      - `_progress` table: `(WAITING, 1, 2, False) == 30`,
        `(WAITING, 3, 2, False) == 60` (capped), `(WAITING, 0, 1, False) == 0`,
        `(WAITING, 1, 2, True) == 70`, `(HARVESTING, *, *, *) == 80`,
        `(READY, …) == 100`, `(STALLED, …) is None`, `(PROMPTED, …) == 0`;
      - `_Heartbeat.line()` on a `RoundState` with two working agents whose
        worktrees have 1 and 3 files touched shows `30%`/`60%`, `1f`/`3f`, a
        ten-cell bar each, and a READY agent as `READY (tests 4m)` when its
        last turn's harvest carries `elapsed: 240.0`;
      - the fake round (`tests/_kilo_fake.py`) with `run_tests` on: the
        READY turn's `harvest` dict in `turns.jsonl` and `state.json` has a
        float `elapsed`, and the INFO line for READY ends with `(tests Ns)`.
- [ ] `test_ctrl_c_aborts_writes_state_and_propagates_then_resume_finishes`
      and every other existing `tests/test_contest_runner.py` test
      unmodified and green: `from_dict` on a `state.json` **without**
      `elapsed` still loads.
- [ ] `python3 -m pytest tests -q --timeout=180 && python3 -m pytest tests_bugfix -q --timeout=180` green.

## Out of scope

- Seeing the agent's *own* `pytest` (the self-check) from outside — the
  runner has no hook into the session's shell; KC-21/KC-22 handle what that
  self-check leaves behind (uncommitted work at the stall).
- Persisting the heartbeat lines to a file — `cli.main`'s stderr routing is
  KC-18's contract; a log file is its own ticket.
- Any change to `harvest.py`, `gates.py`, the verdicts or the reason codes.

## Self-check before `append_task.py` (required — every item, in the worktree you submit)

- [ ] `python3 --version` on the judge is **3.10.12**;
      `python3 -c "import tools.contest.runner"` from the repo root. No
      backslash and no nested same-quote inside an f-string expression.
- [ ] Exactly **one** commit on top of the base; only this ticket's work.
- [ ] `git diff --stat <base>..HEAD` names only `tools/contest/runner.py`
      and `tests/test_contest_runner.py` (plus `.smoke_tests/` links).
      Never `epic-tasks/`.
- [ ] Names and signatures are the ticket's, verbatim: `_worktree_files(ws)`,
      `_progress(state, files, median, committed)`; `_Heartbeat.__init__`
      and `run_round`'s signature unchanged; `CollectBridge._shrink`
      byte-identical (not in this file — do not touch `tools/collect_bridge.py`).
- [ ] `python3 scripts/sync_test_tiers.py --check` is clean.
- [ ] The new tests are red without the change.
- [ ] `python3 -m pytest tests -q --timeout=180` then
      `python3 -m pytest tests_bugfix -q --timeout=180`, **sequentially**,
      both green.
