# FL-2 — round 85 — results

**Ticket:** `epic-tasks/85-fl-2-every-git-call-outside-gitmanager-still-dies-on-a-held-index-lock.md`
**Base:** `306c393` (branch `fl-2-85-round`); the ideal lands on `kc` = `f105522`
**Entries:** 12 — eight live kenary free slots (`contest-out/85/`, run from qwen25)
plus four SenSenova hand patches (`fl2/SenSenova6-*.patch`)
**Not entries:** the six `fl2/sonet*` files are **KC-41** (ticket 80, deadline commit),
a different ticket; they are scored in their own round, not here.

## The round

| slot | state | what it left |
|---|---|---|
| agnes-2-5-flash | READY `6544895` (after one `continue`, reason `off_ticket_files`) | the only exported patch |
| mimo-v2-5, step-3-7-flash, glm-4-7-flash, hy3, agnes-3-0-flash | STALLED `no idle after 1800s` | edits, no commit — saved as `fl2/fl2-<agent>.STALLED.diff` (KC-31's case again) |
| laguna-s-2-1 | STALLED `no idle after 1800s` | clean tree |
| nex-n2-5-pro | GAVE_UP after three REWORKs (`no_progress_row, commits_ne_1, no_test_file`) | clean tree |

Three of the five stalled trees do not import at all (below): they stalled mid-edit, and
the harvest would have rejected them anyway.

## Two things the ticket gets wrong

1. **It names two helpers that do not exist.** `runner._dirty_paths` is `_dirty_tree`, and
   `manifest._git` is `_run_git`. Only agnes-3-0-flash renamed its reader to match.
2. **`git status` does not fail on a held lock.** Verified on this box's git 2.34.1: with
   `.git/index.lock` present, `git status --porcelain` exits 0 (it refreshes in memory and
   skips the write); `git add -u` exits 128. So the ticket's (and POSTMORTEM-FL-1 §7 G's)
   "`status` when it refreshes" is not an index writer that collides. The *read-error* half
   of the ticket still stands — a non-repository, a vanished worktree or a corrupt index make
   `status` exit 128, and the base read that as a clean tree. sn68-var1 found this on its own
   and stubbed the collision for the status case instead of pretending.

## Scoring

Black-box first, one entry at a time, `-n0`, nothing else on the box:

- `acceptance_fl2.py` — 19 checks from the ticket's Acceptance list, through the callers the
  ticket names. **Counted, not timed** (POSTMORTEM-FL-1 §7 G, §8): a wrapper around
  `subprocess.run` counts every git attempt and releases the lock right after the first
  attempt that hit it, so "waited once" = 2 attempts, "gave up" = 8, "raised at once" = 1.
  An earlier version of this suite used a 0.5 s timer thread and `elapsed <` bounds; it was
  replaced before scoring, for the reason the postmortem gives. **Base: 7/19.**
- Both roots, `tests` then `tests_bugfix`, `-n 4`, sequentially.
- Code read only after, for the ideal.

| # | entry | imports | acceptance | `tests` | `tests_bugfix` | verdict |
|---|---|---|---|---|---|---|
| 1 | **SenSenova6-8-var1** | ✓ | **19/19** | ✓ | ✓ | **winner** — see below |
| 2 | SenSenova6-8-var2 | ✓ | 19/19 | ✓ | ✓ | a `status` that cannot be *launched* still returns `""`; `GitManager` retry routed through an `attempt=` lambda |
| 3 | SenSenova6-7-var1 | ✓ | 19/19 | ✓ | ✓ | deletes `GitManager._LOCK_RETRIES/_BACKOFF_S/_CONTENTION_RE` — kc's `f105522` tests read them |
| 4 | SenSenova6-7-var2 | ✓ | 19/19 | ✓ | ✓ | same deletion; rewrites operator-facing error text ("stale lockfile left in .git") to satisfy a literal grep |
| 5 | mimo-v2-5 (stalled) | ✓ | 18/19 | ✓ | ✗ 1 | the regex in two files; `delta_validator` now decodes a non-UTF-8 HEAD blob — `test_non_utf8_file_at_head_read_at_head_returns_none` red |
| 6 | agnes-2-5-flash (READY) | ✓ | 17/19 | ✓ | ✓ | `workspace._git` raises `RuntimeError`, not `WorkspaceError` — every caller that catches `WorkspaceError` misses it |
| 7 | step-3-7-flash (stalled) | ✓ | 13/19 | ✗ 11 | ✗ 4 | `GitManager` stale-lock and real-error paths broken; `test_contest_cli.py` run tests red |
| — | agnes-3-0-flash (stalled) | ✗ `IndentationError` runner.py:419 | — | — | — | DQ; also left `GitManager`'s own ladder in place (two ladders) |
| — | glm-4-7-flash (stalled) | ✗ `SyntaxError` git_manager.py:209 | — | — | — | DQ; `GitManager._run` dedented to module level |
| — | hy3 (stalled) | ✗ circular import `git_lock_retry` ↔ `git_manager` | — | — | — | DQ |
| — | laguna-s-2-1 | — | — | — | — | no work |
| — | nex-n2-5-pro | — | — | — | — | no work |

No FL-2 patch carries a hidden second commit (`grep '^From [0-9a-f]\{40\}'`, B.4).

## What separated the four 19/19 entries

All four route every named caller through one shared `run_git`, raise or return a sentinel
for an unreadable tree, and keep the ordinary-failure path at one attempt. What decided it is
the tree they land on: `kc` moved to `f105522` after the round's base, and that commit gave
`GitManager` a `_backoff` seam and rewrote the three FL-1 lock tests to stand in for
`gm._backoff` and read `gm._LOCK_RETRIES` / `gm._LOCK_BACKOFF_S`. The ticket says those tests
must pass untouched.

- **SenSenova6-8-var1** keeps the three class constants as aliases of the shared ones and
  *passes them through* (`retries=self._LOCK_RETRIES, backoff_s=self._LOCK_BACKOFF_S`), so a
  class-level override still works — the only entry whose `GitManager` needs one line to take
  kc's seam. It also found the `git status` fact, typed the read error (`TreeReadError`),
  caught it at both runner call sites with a warning, and kept `delta_validator`'s
  "undecodable HEAD = no baseline" contract by passing `encoding`/`errors` through untouched.
- sn68-var2 keeps the constants too, but its `run_git` takes an `attempt=` callable, and a
  status that cannot be launched still comes back as `""`.
- Both sn67 entries delete the constants.

**Draft for the ideal: SenSenova6-8-var1.** Nothing is lifted from the others.

## The ideal — `30458eb` on `kc`

SenSenova6-8-var1 cherry-picked onto `f105522`; one conflict in `git_manager.py` (kc's
`_backoff` against the entry's delegation). Polish:

1. `run_git(..., sleep=)` plus a module `_backoff` seam. `GitManager._run` passes
   `sleep=self._backoff`, so `f105522`'s three lock tests pass untouched, and no test patches
   the `time` module.
2. `run_git`'s default `timeout` is `None`, not 60 s: `workspace._git` and `gates.git` never
   had a deadline, and the ticket's Out of scope rules out changing what callers do.
3. `tests/test_git_run_index_lock.py` counts backoffs instead of timing them — an autouse
   fixture records every wait, and the "released" tests release the lock inside the first
   backoff (the ordering FL-1 made exact). No `threading`, no `time`, no `elapsed`.
4. The runner case asserts the tree is read under a held lock with no wait, instead of
   asserting that it waited.
5. Docstrings in `git_run.py` and `workspace._git` no longer list `status` as an index writer.

**Verification**

- acceptance 19/19; `sync_test_tiers --check` clean (smoke=180, regression=31);
  `collect_bridge.py` byte-identical to kc; `tests` and `tests_bugfix` green.
- Regression injection (`inject_regressions.py`, anchored, B.2): six injections — the ladder
  collapsed to one attempt; `workspace._git`, `gates.git`, `manifest._run_git` each forced to
  `retries=1`; `_dirty_tree`'s read-error raise removed; `GitManager` no longer passing its
  `_backoff`. Every one turns red and back to green on restore, **both** with the judge suite
  and with the ideal's own tests alone (`--own-only`).
- Stress: the FL-1 runbook §4 command (4 suites, 4 × `-n 8`, 32 workers on 8 cores),
  **12 passes, 48 suite runs, 187 920 test executions, 0 failures** (13:01–13:40;
  24 × `tests` 4952 passed, 24 × `tests_bugfix` 2878 passed; per-run 78–212 s). Logs kept,
  untracked, in `fl2/stress-ideal/` (`stress.sh` is the loop).

**Landed:** `30458eb` on `kc` (fast-forward from `f105522`).

## Not taken

- sonet4's "fix race in `test_three_questions_in_one_turn_stall_and_abort`" is a KC-41 fix:
  on `kc` the stall path never reads the tree (`_dirty_tree` is called at idle, line 692, and
  on resume, 809), so the race it describes does not exist until KC-41's deadline commit
  lands. It belongs with the KC-41 ideal.

## Findings

- The ticket's `_dirty_paths` / `manifest._git` names and its `git status` premise (above).
- Five of eight live slots STALLED at 1800 s with uncommitted work, three of them in trees
  that do not import — KC-31/KC-41 again.
- An operator `pytest tests_bugfix -n 4` in `../testtext5` sits stopped (`T`) since ~07:50.
