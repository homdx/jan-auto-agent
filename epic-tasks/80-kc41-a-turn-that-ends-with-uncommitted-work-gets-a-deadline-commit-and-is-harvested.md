# KC-41 — A turn that ends with uncommitted work gets a deadline commit and is harvested like any other entry

**Status:** queued — after KC-31, with which it shares `export_patches`' evidence and which it supersedes in effect (KC-31 writes the lost work to a file for a human; this ticket puts it back into the round). Found live 2026-09-21 in round 64 and proved by hand on 2026-09-22.
**Severity:** HIGH (round 64 reported **zero** harvested entries while holding two entries that pass all four pytest roots; the loss is not a turn's time, it is the round's result)
**File:** `tools/contest/runner.py` (`run_agent` — the terminal branch at the `if state is not None:` block, `_commits_above`/`_dirty_tree`), `contest.ini`
**Symbol:** `run_agent`, `_deadline_commit` (new), `_dirty_tree`, `_commits_above`, `_harvest`, `ContestConfig.deadline_commit` (new)
**Round:** 80
**Size:** M
**Source:** round 64 (`contest-out/64/`, base `9912b78`, eight agents, ticket `64-kc25-…`). The round's own `state.json` and `turns.jsonl` say: three agents `timeout` at `turn_timeout_sec = 1800`, one `stalled`, two `error`, one `idle` through to a REWORK. Harvested entries: none. On 2026-09-22 the five worktrees that were not empty were squashed to one commit each with `git reset --soft <base> && git commit`, given a one-row `runs/<agent>/PROGRESS.csv` by hand, and passed to `harvest(ws, ticket, run_tests=True)` sequentially. The result is the round the operator never saw:

| agent | deadline commit | verdict | four roots | files | ±  |
|---|---|---|---|---|---|
| `mimo-v2-5` | `24d3e8a` | **READY** | PASS / PASS / PASS / PASS | 5 | +403/−3 |
| `agnes-2-5-flash` | `f01e2c5` | **READY** | PASS / PASS / PASS / PASS | 4 | +273/−7 |
| `step-3-7-flash` | `f91efba` | REWORK | `tests:1✗` | 5 | +305/−6 |
| `hy3` | `cf160da` | REWORK | `tests:3✗` | 5 | +344/−7 |
| `glm-4-7-flash` | `86046dc` | REWORK | `tests:34✗` | 3 | +290/−10 |

All five: `shrink: same`, `off_ticket: 0`, `gate: ok`, one commit. Both READY entries had independently written the same two symbols as the ideal patch that had already landed as `215a740` (`roster_on_offer`, `KiloClient.providers`) — they were finished work that never said so.
**Seen again (2026-09-25, round 92, base `fb2054c`, ticket KC-48):**
`laguna-s-2-1` worked the whole turn and never committed. KC-36 extended the
turn three times, because the tree kept changing: at 3600 s (2 files, 495
lines), 4200 s (507) and 4800 s (510). At 5401 s the tree had been flat for
10 minutes, so the turn ended `STALLED` with
"no idle after 90m (2 files, 482 lines, unchanged for 10m)". The worktree
`rounds/92-laguna-s-2-1` holds `tools/contest/runner.py` +228/−3 and
`tests/test_contest_runner.py` +254, uncommitted. `contest-out/92/laguna-s-2-1/`
has no `.diff` (KC-31), and there was no harvest. So KC-36 buys the turn the
time it asks for, and then the round drops what that time produced. Whether
this diff passes the four roots is not checked here.

**Depends on:** KC-21 (landed — the terminal branch harvests a turn that *has* a commit; this ticket removes the "has a commit" precondition), KC-22 (landed — the continue loop that runs before the deadline is reached), KC-31 (queued — same evidence, the file-export half).
**Also touches:** `tests/test_contest_runner.py`, `tests/_kilo_fake.py`, `tools/contest/harvest.py` (one new fact key, no new reason code), `tools/contest/cli.py` (`export_patches` — a deadline commit exports as a patch, not a `.diff`)

---

## What happens today

`run_agent`, terminal branch (`tools/contest/runner.py`, the `if state is
not None:` block): for a `STALLED` or `ERROR` turn it reads
`above = _commits_above(ws)` and harvests **only `if above:`**. Zero
commits and a dirty tree → no harvest, no verdict, `run.commit = None`,
and the agent lands in its terminal state as though it had produced
nothing. The `HARVESTING` path below it never runs either, because the
turn already ended.

`harvest` itself is stricter still, and correctly so: it wants a
`runs/<agent>/PROGRESS.csv` row for the ticket whose `outcome` is in
`{DONE, FIXED}` and whose `commit` resolves to a sha on the branch. An
agent that ran out of clock wrote neither the commit nor the row, so even
a harvest forced on that worktree would answer `no_progress_row`.

The net effect is that the *only* way work counts is if the model
finishes early enough to commit and to run `append_task.py`. Every turn
killed by the clock is scored as a zero regardless of what is on disk —
which is how a round holding two green entries reported none.

## What must change

1. **`_deadline_commit(ws, *, reason) -> str | None`** — new helper next to
   `_dirty_tree`. When `_commits_above(ws) == 0` and `_dirty_tree(ws)` is
   non-empty: `git add -A -- ':!runs'`, then `git -c user.name=… -c
   user.email=… commit -m "WIP (deadline commit, <reason>): <ticket>"` on
   the agent's own branch, and return the new sha. A clean tree returns
   `None` and nothing is committed. `runs/` is already in `.gitignore`, so
   the PROGRESS row written in step 2 never enters the commit — the
   pathspec is belt and braces.
2. **The PROGRESS row is written by the runner, not asked of the model.**
   After a deadline commit, append one row to `ws.progress_csv`
   (`ticket,finding,outcome,commit,note`; header when the file is new)
   with `outcome = FIXED`, `commit = <the sha>`, and a `note` naming the
   reason. The row is the runner's statement of fact — "this is what was
   on disk when the clock ran out" — not a claim on the model's behalf,
   and the distinction is carried in step 3.
3. **`run.deadline_commit: bool`** on `AgentRun`, serialised to
   `state.json` and reported in `turns.jsonl` alongside `harvest`. A
   `READY` reached from a deadline commit is *not* the same signal as a
   `READY` the model claimed, and every consumer must be able to tell
   them apart: `SUMMARY.md`, the round's table, and the judge.
4. **The terminal branch harvests either way.** Replace `if above:` with:
   commit first when there is nothing committed (step 1), then harvest
   whenever there is now a commit. The rest of the branch is unchanged —
   `verdict.commit or _head_sha(ws)`, the `READY` promotion, the note.
5. **`export_patches`** treats a deadline commit as a commit: the agent
   gets a `<agent>.STALLED.patch` as KC-21 names it, not KC-31's
   `<agent>.STALLED.diff`. KC-31's `.diff` remains for the case this
   ticket cannot help — a terminal turn whose worktree is genuinely clean.
6. **`deadline_commit = true`** in `contest.ini`, on by default. `false`
   restores today's behaviour exactly.

## Acceptance

- [ ] `tests/test_contest_runner.py`: a turn that ends `STALLED` with two
      edited files and no commit → exactly one commit on the branch, a
      `PROGRESS.csv` row naming it, a harvest ran, `turns.jsonl` carries
      both `harvest` and `deadline_commit: true`.
- [ ] The same turn with a **clean** worktree → no commit, no row, no
      harvest, `deadline_commit` absent; identical to today.
- [ ] A turn that ends `STALLED` **with** a commit → unchanged from KC-21:
      one commit, no second one, `deadline_commit: false`.
- [ ] A deadline-committed worktree whose tests pass reaches
      `AgentState.READY`, and `state.json` shows both `READY` and
      `deadline_commit: true`.
- [ ] `deadline_commit = false` in the config → byte-identical behaviour
      to the tree before this ticket (regression guard).
- [ ] `export_patches` writes `<agent>.STALLED.patch` for a
      deadline-committed agent and `<agent>.STALLED.diff` only for a clean
      one (KC-31's case).
- [ ] **Replay against real data, required.** Round 64 is on disk and is
      the exact case this ticket exists for. Add
      `contest-bench/kc41/replay64.py`: point it at `../rounds/64-*` and
      `contest-out/64/state.json`, apply steps 1–4 offline, and assert the
      table in *Source* comes back — two `READY`, three `REWORK` with
      `tests_failed`, five commits, `off_ticket: 0` and `shrink: same`
      throughout. Record it in `contest-bench/kc41/RESULTS.md` in
      `contest-bench/kc6/RUNBOOK.md` §10's style. The four pytest roots
      run **sequentially**; the 2026-09-22 pass took ~65 min wall, almost
      all of it `glm-4-7-flash` (see *Out of scope*).
- [ ] `python3 -m pytest tests -n 4 -q --timeout=180 && python3 -m pytest tests_bugfix -n 4 -q --timeout=180` green (sequentially).

## Out of scope

- Deciding whether a deadline commit may *win* a round. It qualifies as an
  entry and carries `deadline_commit: true`; what the judge does with that
  flag is the judge's ticket, not this one. (Evidence for allowing it: both
  round-64 READY entries were complete work. Evidence for the other side:
  none yet.)
- Asking the model to explain the committed state — that is KC-40.
- Any change to the clock that produced the deadline — KC-36.
- Any change to `harvest`'s reason codes or to its `{DONE, FIXED}` rule.
  The runner satisfies the existing contract; it does not relax it.
- `glm-4-7-flash`'s entry made the `tests` root take **2609 s** against
  ~260 s for every other entry in the same replay. That is its own code's
  problem, not this ticket's, and not the known KC-38 flake; it needs its
  own ticket if it is ever reproduced against code that lands.

## Ground rules (same as every round)

- Do not touch `CollectBridge._shrink`.
- No test starts a real `kilo` or calls a live provider — `contest-bench/`
  is not `tests/` and is run by hand.
- Do not edit `epic-tasks/` (other than this round's own ticket file).
- One commit, no push; a test ships with the change and fails without it.
