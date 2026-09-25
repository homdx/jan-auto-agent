# KC-60 — the harvest tests the commit it scores, not the working tree around it

**Status:** queued — found 2026-09-25 judging round 103 (FL-9): `agnes-2-0-flash` and `step-3-7-flash` were harvested `READY` with every root green, and their handed-in patches fail 4 tests on `kc`.
**Severity:** HIGH (a `READY` verdict and a green heartbeat for a patch that is red as handed in; the operator finds out only when landing it)
**File:** `tools/contest/harvest.py`, `tools/contest/gates.py`
**Symbol:** `harvest` (the `run_tests` block), `gates.run_tests_detail`
**Round:** 107
**Size:** S
**Source:** round 103, `contest-out/103/state.json` and the judge's run (`scripts/judge_round.sh`, `kc` `e4bea5b`):

Both agents created the tier symlink `.smoke_tests/test_gate1_find_def.py`
with `scripts/sync_test_tiers.py`, and never `git add`ed it. The worktree still
shows it: `git status --porcelain` → `?? .smoke_tests/test_gate1_find_def.py`.

The harvest runs `run_tests_detail(str(ws.path))` in that worktree, so the
untracked symlink was there. `test_tier_symlinks.py` and `test_precommit_hook.py`
passed, and the verdict was `READY` (agnes-2-0 after 1507 s of tests).

The patch the round exports (`git format-patch <base>..HEAD`) has no symlink.
On a clean checkout: `4 failed` —
`test_precommit_hook.py::test_hook_passes_on_the_real_repository`,
`test_tier_symlinks.py::test_tiers_cover_every_test_file`,
`::test_sync_script_check_mode_is_clean`, `::test_sync_script_is_idempotent`.

The same hole works the other way too. An uncommitted edit that fixes a
failing test makes the harvest green for a commit that does not contain the
fix. No reason code looks at `git status`: the list is `no_progress_row`,
`progress_not_done`, `no_commit`, `commit_not_on_branch`, `pushed`,
`no_test_file`, `shrink_changed`, `off_ticket_files`, `tests_failed`.

**Depends on:** KC-16 (`run_tests` in the harvest).
**Also touches:** KC-41 (a deadline commit of uncommitted work; that commit, too, must be what gets tested), KC-57 (the harvest's budget).

---

## What must change

### 1. The roots run on the commit

With `run_tests`, the harvest checks the scored commit out into a throwaway
detached worktree (`git worktree add --detach <tmp> <commit>`; removed in
`finally`) and runs the roots there. It does not run them in the agent's
worktree. The agent's tree is left exactly as it was: no stash, no clean, no
checkout. The mechanical checks that read files (`no_test_file`,
`shrink_changed`, `off_ticket_files`) already read the commit and stay as they
are.

### 2. What is left outside the commit is named

A new non-blocking reason, `uncommitted_files`, lists the `git status --porcelain`
entries, at most 10 of them. What counts as noise is the repo's own
`.gitignore`, never a list in the runner: `git status` does not report
ignored files. It goes into the rework prompt, so a `REWORK` for
`tests_failed` says why: "`<path>` is untracked, and the commit you handed
in does not contain it".

### 3. Nothing here knows this repo's layout

No tier names (`.smoke_tests`, `.regression_tests` or anything else), no
`runs/`, no test-root names: the fix is "test what `git` has in the commit".
A repo whose tiers are `.smoke_fast/` symlinks, or that has no tiers at all,
gets the same behaviour. Committed relative symlinks survive
`git worktree add`, and untracked ones are simply not there. This was checked
by hand on a scratch repo with a `.smoke_fast/` tier: in the agent's tree
`3 passed`, on the commit `1 failed`, and the tracked tier link still ran
green.

## Out of scope

- Committing for the agent (KC-41).
- The tier tool itself: `sync_test_tiers.py` makes the link, the agent must add it.

## Acceptance

- [ ] A worktree whose commit lacks a file that only an untracked file makes pass: the harvest is `tests_failed`, and `uncommitted_files` names the file. The test builds its own tier dir under a name that exists nowhere in this repo (e.g. `.smoke_fast/`).
- [ ] A file matched by the repo's `.gitignore` is not listed in `uncommitted_files`.
- [ ] An uncommitted edit that would fix a failing test does not turn the verdict `READY`.
- [ ] The agent's worktree is byte for byte unchanged after the harvest: `git status`, `git stash list` and the file mtimes are the same.
- [ ] The throwaway worktree is gone after the harvest, on both success and exception.
- [ ] `run_tests=False`: nothing changes.
- [ ] `tests` and `tests_bugfix` green, sequentially; `CollectBridge._shrink` byte-identical.
