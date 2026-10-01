# 132 — refuse workspace reset when git rev-list or status fails instead of reading it as clean

**Status:** landed — round 132, winner sensenova-6-8-flash-lite-var2 (19/19 on contest-bench/132), 8c58a1d as-is
**Severity:** HIGH
**File:** tools/contest/workspace.py
**Symbol:** _commits_above, _dirty_outside_runs, prepare_round, reset_worktree, reset_clone
**Round:** 132
**Size:** S
**Also touches:** tests/test_contest_workspace.py

---

## Why

KC-23 / FL-2: `_commits_above` runs `git rev-list --count <base>..HEAD` with `check=False` and returns 0 when `proc.returncode != 0`, and `_dirty_outside_runs` runs `git status --porcelain --untracked-files=all` with `check=False` and never reads `proc.returncode`, so a failed status yields empty output that reads as a clean tree. Both callers in `prepare_round` (the worktree branch near `commits = _commits_above(path, base_sha)` and the clone branch after the fetch) then skip `_refuse_to_reset` and go to `checkout -B` and `clean -fdx`, deleting the agent's commits and edits the guard exists to keep. FL-2's own rule, already followed by `runner._dirty_tree` (it raises `TreeReadError`), is that a failed git status is a read error, not a clean tree.

## What to build

From `tools/contest/workspace.py`:

- `_commits_above(worktree, base_sha)`: branch `if proc.returncode != 0:` currently `return 0` → raise `WorkspaceError` naming `worktree`, the failed command `rev-list --count <base>..HEAD`, `proc.stderr.strip()`, and the text "pass --fresh to discard". Success branch unchanged: `text = proc.stdout.strip()` then `return int(text) if text.isdigit() else 0` (fallback `0` when not a digit).
- `_dirty_outside_runs(worktree)`: currently never reads `proc.returncode` and returns `[line for line in proc.stdout.splitlines() if not _is_runs_scratch(line)]`. Add branch: when `proc.returncode != 0`, raise `WorkspaceError` naming `worktree`, the failed command `status --porcelain --untracked-files=all`, `proc.stderr.strip()`, and "pass --fresh to discard". Success return unchanged: the filtered list that excludes `runs/` via `_is_runs_scratch` (empty list when no lines).
- `reset_worktree`: in the `else` block (`path.exists()` and `path.resolve() not in owned` already raised), `commits = _commits_above(path, base_sha)` and `dirty = _dirty_outside_runs(path)` — with `force` (`--fresh`) neither is called, since the reset discards everything anyway; without `force`, a `WorkspaceError` from either propagates with no `try`. The existing guard `if (commits or dirty) and not force:` calling `_refuse_to_reset(path.resolve(), branch, round_no, commits, dirty)` is unchanged for the found-work case. With `force` (operator `--fresh`), `_git(path, ["checkout", "-f", "-B", branch, base_sha])` then `_git(path, ["clean", "-fdx", "-e", "runs/"])` and `_empty_runs_dir(path, agent)` proceed. On git success with `commits == 0` and `dirty == []`, guard skipped, silent reset; `runs/` ignored via `-e runs/` and `_empty_runs_dir`.
- `reset_clone`: after `_git(path, ["fetch", "-q", "origin"])` and the `_rev_parse(path, f"{base_sha}^{{commit}}") is None` base check, same `commits = _commits_above(path, base_sha)` and `dirty = _dirty_outside_runs(path)`, both skipped under `force` exactly as in `reset_worktree`, same `if (commits or dirty) and not force:` guard, same `force` `checkout -B` / `clean -fdx -e runs/` / `_empty_runs_dir` path, same success behavior.
- `prepare_round`: dispatches to `reset_worktree` when `_workspace_kind(config) == _KIND_WORKTREE` else `reset_clone`; the `WorkspaceError` propagates out of `prepare_round` unchanged.
- Parameter the brief names that is never read: none — `base_sha` is read by `_commits_above`; `path`/`worktree` is read; `force` (the operator's `--fresh`) is read at `if (commits or dirty) and not force:` in both `reset_worktree` and `reset_clone`.

From `tests/test_contest_workspace.py` (using fixtures `repo`, `config` with `workspace_kind="worktree"`, `_base_sha`, `prepare_round`, `WorkspaceError`, and monkeypatching `tools.contest.workspace._git`):

- (1) Worktree with one commit above the base (write `thing.py`, `git add`, `git commit` as in `test_prepare_round_refuses_a_worktree_with_a_commit_above_the_base`) where `_git` is monkeypatched so the `rev-list --count` call returns `returncode 128` → `prepare_round(repo, config, 40, base)` raises `WorkspaceError` and the commit is still there (`thing.py` exists, `rev-parse HEAD` != base).
- (2) Worktree with an edited tracked file (write `thing.py` as in `test_prepare_round_refuses_a_worktree_with_uncommitted_edits`) where `_git` is monkeypatched so the `status --porcelain --untracked-files=all` call returns `returncode 128` with empty stdout → `prepare_round` raises `WorkspaceError` and the edit is still on disk.
- (3) The same two monkeypatched scenarios called with `force=True` → the reset happens (`rev-parse HEAD == base`, file/edit gone, `status --porcelain` empty).
- (4) The existing `test_prepare_round_resets_a_clean_worktree_at_the_base` must still pass (clean-worktree reset unchanged: `runs/` scratch emptied, `rev-parse HEAD == base`).

## Acceptance

```bash
python3 -m pytest tests/test_contest_workspace.py -n 4 -q
python3 -m pytest tests_bugfix -n 4 -q
```

## Rules

- Add the four cases above to `tests/test_contest_workspace.py` (already named `test_<area>_<feature>.py`); tier them via `python3 scripts/sync_test_tiers.py` (never by hand) so they appear under `.smoke_tests/` and `.regression_tests/`, then verify with `python3 scripts/sync_test_tiers.py --check`.
- Keep `tests_bugfix` green: `python3 -m pytest tests_bugfix -n 4 -q`.
- Match the heavy explanatory comment style of `tools/contest/workspace.py`; keep standard-library-only plus `tools.git_run.run_git`; type hints on public functions.
- Commit subjects prefixed by ticket (`132: …`), one concern per commit, stating behavior before/after and the exact test command run (`python3 -m pytest tests/test_contest_workspace.py -n 4 -q`).

## Result (round 132)

7 agents, all READY, all 19/19 on `contest-bench/132/test_bench_132.py` (the base fails 10 of the 19: a failed `rev-list`/`status` read as 0 commits / a clean tree, for both the worktree and the KC-59 clone path). Every entry raised `WorkspaceError` from both reads and skipped them under `force`; none touched `runner._commits_above` (a different function).

Picked on the message and the docs: sensenova-6-8-flash-lite-var2 says what cannot be told ("cannot tell whether the worktree holds commits above the base"), falls back to stdout when stderr is empty, and documents the refusal in `prepare_round`. Taken as-is; no follow-up needed.
