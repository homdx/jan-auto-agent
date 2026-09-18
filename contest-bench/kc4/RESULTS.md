# KC-4 contest — black-box results (9 entries)

Method: `contest-bench` (see `contest-bench/README.md`) applied to KC-4 —
each entrant's `tools/contest/workspace.py` applied at base `1b34384` (post
KC-2), exercised against a **disposable sandbox git repo** built fresh per
scenario (2 commits: init + a committed `epic-tasks/`), never this repo and
never each entry's own test file. Checks are black-box: real `git` state
(HEAD sha, `worktree list`, `branch --list`, `status --porcelain`), never the
entrant's own assertions. Harness: `contest-bench/kc4/run_one_kc4.py` +
`run_all_kc4.py`. 12 scenarios, 3 rounds of "read the code of the tied top
group, add a scenario that splits them" (rounds 1–2 below; round 3 found
nothing further to add).

## Score table

| # | scenario | what it checks | agnes-2-5-flash | deepseek-v4-1-flash | glm4-7 | hy3 | laguna-s-2-1 | sn67-var1 | sn68-var1 | sn68-var2 | sn68-var3 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | fresh_round | N worktrees at base sha, right branch, empty `runs/<agent>` | 8/8 | 8/8 | 8/8 | 8/8 | 8/8 | 8/8 | 8/8 | 8/8 | 8/8 |
| 2 | rerun_after_dirty_crash | dirty file + stray `PROGRESS.csv` → back to base, empty `runs/`, clean | 4/4 | 4/4 | **2/4** | 4/4 | 4/4 | 4/4 | 4/4 | 4/4 | 4/4 |
| 3 | foreign_folder_refused | non-worktree folder at the path → `WorkspaceError`, left untouched | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 |
| 4 | untracked_epic_tasks_refused | untracked file under `epic-tasks/` at base → raises, names the reason | 3/3 | 3/3 | **1/2** | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 |
| 5 | unresolvable_base | bad `base_ref` → raises | 2/2 | 2/2 | 2/2 | 2/2 | 2/2 | 2/2 | 2/2 | 2/2 | 2/2 |
| 6 | idempotent_reset_worktree | `reset_worktree` called twice → same sha, clean | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 |
| 7 | attach_clone_dirty_then_force | dirty clone raises without `force`, resets with it | 4/4 | 4/4 | 4/4 | 4/4 | 4/4 | 4/4 | 4/4 | 4/4 | 4/4 |
| 8 | attach_clone_missing_base | clone lacking base sha → raises after fetch | 2/2 | 2/2 | 2/2 | 2/2 | 2/2 | 2/2 | 2/2 | 2/2 | 2/2 |
| 9 | remove_round_cleans_up | no leftover worktrees/branches for the round | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 |
| 10 | **stale_branch_recreated** *(round 2)* | worktree removed externally, branch left behind → `reset_worktree` recreates it at base with `-B`, same branch name | **2/3 CRASH** | 4/4 | **2/3 CRASH** | 4/4 | 4/4 | 4/4 | 4/4 | 4/4 | 4/4 |
| 11 | worktree_of_other_repo *(round 2)* | path is a real worktree, but of a **different** repo → raises, never touches it | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 |
| 12 | repo_checkout_never_touched | this repo's own HEAD/status never move | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 |
| | **total** | | **40/42** | **42/42** | **36/42** | **42/42** | **42/42** | **42/42** | **42/42** | **42/42** | **42/42** |

## Where the two losers actually broke

**agnes-2-5-flash** (round 2 found this) — `reset_worktree`'s "path absent"
branch always runs `git worktree add <path> -b <branch> <base_sha>`, with no
check for a stale `contest/<NN>/<agent>` branch left over from a worktree
that was removed externally (e.g. by `remove_round`, or by hand). `git`
refuses `-b` on a branch that already exists, so the second `prepare_round`
of the same round crashes instead of recreating it — the exact "rerun after
a crash" case the ticket is about, just one step later than the other
crash-recovery scenario (#2) that agnes does pass.

**glm4-7** — three independent misses in the same file:
1. same stale-branch crash as agnes, but from a broken *fix attempt*:
   `git branch -M <branch> <base_sha>` (a **rename**, first argument old name
   second new name) is used where a reset-to-sha was intended — it tries to
   rename the branch to the literal string `<base_sha>`, which fails against
   the already-existing branch and raises `WorkspaceError` from the `except`
   clause around it;
2. `_check_epic_tasks` is never called from `reset_worktree`/`prepare_round`
   (`# Step 2: check epic-tasks/ is clean at this base` — a comment, no
   code) — an untracked file under `epic-tasks/` at the base sha is silently
   accepted;
3. on rerun, `clean -fdx -e runs/` runs *before* `checkout -B`, on the
   **previous** branch/commit rather than after landing on the base — a file
   the previous round added and committed on the round branch (not just left
   dirty) survives the "reset", so `runs_cleared`/`status_clean` both fail
   after a rerun that follows a `git add`+something on that branch.

## Round 1 finding that turned out to be a harness bug, not a product bug

`foreign_folder_refused`'s first pass reported 3 failures (glm4-7,
sn68-var2, sn68-var3) — all from the harness guessing the path as
`NN-agent` (zero-padded) when those three format it as `N-agent` (no
padding for round numbers <10; the ticket's own example, `40-laguna`, cannot
distinguish the two conventions since 40 is already two digits). Fixed by
deriving the real path from a live round instead of guessing it (see
`run_one_kc4.py`, scenario `foreign_folder_refused`); re-run cleared all
three. Padding is cosmetic — `scripts/contest_reset.sh`'s printed line is
the only place it's user-visible — so it is **not** scored.

## Recommendation

Seven of nine (`deepseek-v4-1-flash`, `hy3`, `laguna-s-2-1`, `sn67-var1`,
`sn68-var1/2/3`) pass all 42 checks. Among those, code size for the same
acceptance list ranges from 401 lines (`hy3`) to 634 (`sn67-var1`) — `hy3`
and `laguna-s-2-1` are the two under 450 lines; `hy3`'s module docstring and
comment voice is the closest match to this repo's existing
`tools/contest/{kilo_client,roster,policy}.py` (explains *why* against the
probe/runbook, not *what*). Proposed ideal-patch base: **`hy3`**, diffed
against the other six full-scorers for any check-relevant behavior it lacks
before finalizing — table above is black-box only; code reading comes next,
per the standing contest-bench rule (code is read only after scoring).
