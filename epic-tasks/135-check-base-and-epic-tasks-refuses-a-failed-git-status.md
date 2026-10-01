# 135 — _check_base_and_epic_tasks refuses a failed git status instead of reading it as clean

**Status:** open
**Severity:** MEDIUM
**File:** tools/contest/workspace.py
**Symbol:** _check_base_and_epic_tasks
**Round:** 135
**Size:** S
**Also touches:** tests/test_contest_workspace.py

---

## Why

`_check_base_and_epic_tasks` calls `_git(repo, ["status", "--porcelain", "--untracked-files=all", "--", "epic-tasks"], check=False)` and only inspects `proc.stdout.strip()`. When git fails (e.g. an index lock, returncode `128`), `stdout` is empty, so the function returns the base sha and the round starts as if `epic-tasks/` were clean — on possibly stale tickets. The sibling reads `_commits_above` and `_dirty_outside_runs` already raise `WorkspaceError` on a non-zero `returncode`; this read must match so a failed status is never silently treated as clean.

## What to build

From the source of `tools/contest/workspace.py`, `_check_base_and_epic_tasks(repo: Path, base_ref: str) -> str` shows these concrete cases:

- `_rev_parse(repo, base_ref)` → `None`: raises `WorkspaceError(f"base ref {base_ref!r} does not resolve to a commit in {repo}")` (unchanged).
- `_git(repo, ["status", "--porcelain", "--untracked-files=all", "--", "epic-tasks"], check=False)` → `proc` (unchanged call; the defect is that `proc.returncode` is never read).
- NEW case: `proc.returncode != 0` → raise `WorkspaceError` naming `git status --porcelain --untracked-files=all -- epic-tasks`, `({proc.returncode})` (e.g. `128`), and `proc.stderr.strip() or proc.stdout.strip()`, stating it cannot tell whether `epic-tasks/` is clean; mirrors the `proc.returncode != 0` raise in `_commits_above` (message `f"git rev-list --count {base_sha}..HEAD in {worktree} failed ({proc.returncode}): ..."`) and in `_dirty_outside_runs` (message `f"git status --porcelain --untracked-files=all in {worktree} failed ({proc.returncode}): ..."`).
- Case: `proc.returncode == 0` and `proc.stdout.strip()` non-empty → raise `WorkspaceError(_EPIC_TASKS_DIRTY_REASON)` (unchanged; `_EPIC_TASKS_DIRTY_REASON` is the module constant).
- Case: `proc.returncode == 0` and `proc.stdout.strip()` empty → `return base_sha` (unchanged).

Return values: `str` (base sha) on the clean path; `WorkspaceError` raised on unresolvable base, on dirty `epic-tasks/`, and now on failed `git status`.

Tests to add in `tests/test_contest_workspace.py` (simulate the failure by monkeypatching `workspace._git` so only the `status ... -- epic-tasks` call returns `CompletedProcess(..., 128, "", "fatal: index.lock")`; do not assert a specific code beyond "non-zero"):

- (1) failed status → `_check_base_and_epic_tasks` raises `WorkspaceError`, and the message names `epic-tasks` and the code.
- (2) the same failure through `prepare_round` → `WorkspaceError`, and no worktree is created under the rounds dir.
- (3) unchanged: a dirty `epic-tasks/` (untracked `epic-tasks/x.md`) still raises with `_EPIC_TASKS_DIRTY_REASON`; a clean repo still returns the base sha.

## Acceptance

```bash
python3 -m pytest tests/test_contest_workspace.py -n 4 -q
```

```bash
python3 -m pytest tests_bugfix -n 4 -q
```

## Rules

- Edit `tools/contest/workspace.py` only within `_check_base_and_epic_tasks`; keep the existing `from __future__ import annotations` and the already-defined `WorkspaceError`; match the module's explanatory comment style (no new formatter).
- Add the failed-status test to `tests/test_contest_workspace.py` (existing file already matches `tests/test_<area>_<feature>.py`); give it a one-line docstring.
- Run `python3 scripts/sync_test_tiers.py` to tier the new test (new feature tests in `tests/` must be tiered; never edit tier symlinks by hand).
- Run `python3 -m pytest .smoke_tests/` as the fast pre-commit gate.
- Commit with a terse subject prefixed by the ticket (`135: ...`) describing the behavior change (raise `WorkspaceError` on failed `epic-tasks/` status), one concern per commit.
