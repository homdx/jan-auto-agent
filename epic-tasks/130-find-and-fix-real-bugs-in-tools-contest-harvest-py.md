# 130 — find and fix real bugs in tools/contest/harvest.py

**Status:** landed `36dd4c4`+`abe73fd` on 2026-10-01 — round 130, winner agnes-3-0-flash 6/6 on contest-bench/130 taken as-is; 4 of 9 entries 6/6, laguna-s-2-1 DEAD
**Severity:** MEDIUM
**File:** tools/contest/harvest.py
**Symbol:** Reason, Harvest, _progress_rows, _is_ancestor, _at_or_under_base, _resolve_claim, _status_lines, _commit_worktree, _drop_worktree, _branch_commit, _last_tail_section, _tests_slow_reason, _uncommitted_reason, harvest, rework_message
**Round:** 130
**Size:** M
**Also touches:** tests/test_contest_harvest.py

---

## Why

`tools/contest/harvest.py` produces the READY/REWORK verdict that decides contest worktrees. Real bugs in its branches, return values, config keys or fallbacks can mis-score agents. The brief tasks us with finding and fixing such bugs, each with a test in `tests/` that fails before and passes after, with no behavior change without a failing test and no refactors.

## What to build

Concrete cases shown in `tools/contest/harvest.py` — examine each for real bugs; any fix must ship a test in `tests/` (use `tests/test_contest_harvest.py`) that fails before and passes after:

Branches and return values by name:
- `_progress_rows(progress_csv)`: branch on `progress_csv.is_file()`; returns `[]` or `list(csv.DictReader(fh))`.
- `_is_ancestor(path, commit)`: returns `subprocess.run(["git","merge-base","--is-ancestor",commit,"HEAD"]).returncode == 0`.
- `_at_or_under_base(ws, commit)`: reads `getattr(ws, "base_sha", "") or ""`; returns `False` when empty, else `subprocess.run(["git","merge-base","--is-ancestor",commit,base]).returncode == 0`.
- `_resolve_claim(path, claimed)`: branch on `_SHA_RE.fullmatch(claimed)`; returns `None` when no match, else `git(str(path), "rev-parse", "--verify", "--quiet", f"{claimed}^{{commit}}") or None`.
- `_status_lines(ws)`: `try run_git(["git","--no-optional-locks","status","--porcelain","--untracked-files=all"])`; on `OSError` or non-zero `returncode` returns `[]`; filters lines under `ws.progress_csv.parent`; returns remaining lines.
- `_commit_worktree(ws, commit)`: `try tempfile.mkdtemp` → on `OSError` returns `(None, None, f"no temp directory for the checkout: {exc}")`; `git worktree add -q --detach target commit`; on error calls `_drop_worktree` and returns `(None, None, error)`; else returns `(target, parent, "")`.
- `_drop_worktree(ws, parent)`: returns immediately when `parent is None`; `git worktree remove --force`; `shutil.rmtree`; if remove failed `git worktree prune`.
- `_branch_commit(path, facts)`: returns `None` when `facts.get("commits") != 1`; tries `git rev-parse --verify --quiet f"{sha}^{{commit}}"` (catches `OSError`, `subprocess.TimeoutExpired`), falls back to `git rev-parse HEAD or None` (catches same), returns full sha or `None`.
- `_last_tail_section(tail)`: scans reverse for line starting with `"--- "`; returns `tail[i:]` or `tail`.
- `_tests_slow_reason(budget, tail)`: `kind, ids = _slow_tests(_last_tail_section(tail))`; when `kind == "running"` keeps `ids[-1:]`; builds `text` within `TEXT_LIMIT`; returns `Reason("tests_slow", text, blocking=True)`.
- `_uncommitted_reason(lines)`: returns `Reason("uncommitted_files", text, blocking=False)`; one-line vs multi-line text, truncating at `UNCOMMITTED_MAX` with `+N more`.
- `harvest(ws, ticket_path, *, run_tests=False, budget_sec=0.0, waited=0.0, ahead=0, test_lock=None)`:
  - `budget = max(0.0, float(budget_sec or 0))`; `ticket = Path(ticket_path).name`; `declared = declared_files(ticket_path)`.
  - claim loop matches `row.get("ticket") == ticket`; `claimed_commit` from `claim.get("commit")`, `outcome` upper-cased.
  - `claim is None` → `Reason("no_progress_row")`; else `outcome not in _DONE_OUTCOMES` → `Reason("progress_not_done")`; `not claimed_commit` → `Reason("no_commit")`; `_SHA_RE.fullmatch` fails → `Reason("commit_not_on_branch")` (shape); else `sha = _resolve_claim`, `claimed_base = sha is not None and _at_or_under_base`; `claimed_base and _is_ancestor` → `Reason("commit_not_on_branch")` (base); `sha is not None and _is_ancestor` → `resolved = sha`; else `Reason("commit_not_on_branch")` with `head`-based fix hint.
  - `facts = judge_worktree(ws.agent, str(ws.path), ws.base_sha, list(declared), want_tests=False)`; sets `facts["deadline_commit"]` from `facts.get("commits") == 1` and `git log -1 --format=%ce HEAD == DEADLINE_COMMIT_EMAIL`.
  - `"commits" not in facts` → `Reason("commits_ne_1")` (not git worktree); else `facts["commits"] != 1` → `Reason("commits_ne_1")`; `facts["pushed"] == "yes"` → `Reason("pushed")`; `facts["test_files"] == 0` → `Reason("no_test_file")`; `facts["shrink"] in ("CHANGED","GONE")` → `Reason("shrink_changed")`; `facts["off_ticket"]` truthy → `Reason("off_ticket_files", blocking=False)`.
  - `commit = resolved if facts.get("commits") == 1 else None`; when `commit is None and (claim is None or claimed_base)` → `commit = _branch_commit(ws.path, facts)`.
  - `run_tests` branch: blocking reasons present → `facts["tests_run"] = f"skipped: {code}"`; else `outside = _status_lines(ws)`, `"commits" in facts` → `_commit_worktree(ws, resolved or "HEAD")` else `(None, None, f"{ws.path} is not a git worktree")`; `target is None` → `facts["tests_run"]="checkout✗"` and `Reason("tests_failed")`; else `run_tests_detail` under `test_lock`, `finally _drop_worktree`, `facts["tests_run"]=summary`, `"budget✗" in summary` → `_tests_slow_reason`, `"✗" in summary` → `Reason("tests_failed")` with tail; `outside` → `_uncommitted_reason`.
  - `verdict = "REWORK" if any(r.blocking for r in reasons) else "READY"`; returns `Harvest(verdict, reasons, commit, facts, elapsed, waited, ahead)`.
- `rework_message(h, attempt, max_rework)`: builds blocking bullets; when `facts["tests_run"]` starts with `"skipped:"` appends note; appends non-blocking `noted`; returns joined lines.

Config keys read: `ws.base_sha`, `ws.agent`, `ws.branch`, `ws.path`, `ws.progress_csv`; imported names `BRIDGE`, `DEADLINE_COMMIT_EMAIL`, `declared_files`, `git`, `judge_worktree`, `run_tests_detail`, `_slow_tests` from `tools.contest.gates`; `Workspace` from `tools.contest.workspace`; `run_git` from `tools.git_run`. Module constants: `REASON_CODES`, `_DONE_OUTCOMES`, `TEXT_LIMIT`, `UNCOMMITTED_MAX`, `_SHA_RE`.

Fallbacks by name: `_resolve_claim` → `None`; `_branch_commit` → `git rev-parse HEAD` then `None`; `_commit_worktree` → no fallback to `ws`; `_status_lines` → `[]`; `harvest` commit → `_branch_commit` when claim `None`/`claimed_base`; `run_tests` skipped when blocking; `_tests_slow_reason` truncates node ids to `TEXT_LIMIT`.

The brief names no parameter of `tools/contest/harvest.py`, so none is noted as never read.

## Acceptance

```bash
python3 -m pytest tests/test_contest_harvest.py -v
```

Every bug fixed must have a test in `tests/test_contest_harvest.py` (or a new `tests/test_*.py`) that fails before the fix and passes after. The full suite must stay green:

```bash
python3 -m pytest tests/
```

## Rules

- Enable the pre-commit hook: `git config core.hooksPath githooks`.
- For each real bug fixed, add a test in `tests/` (e.g. `tests/test_contest_harvest.py`) that fails before the fix and passes after; no behavior change without such a failing test; no refactors.
- Regenerate tier symlinks with `python3 scripts/sync_test_tiers.py` (never by hand); verify with `python3 scripts/sync_test_tiers.py --check`.
- Run the fast pre-commit gate: `python3 -m pytest .smoke_tests/`.
- Commit with subject prefixed by ticket (`130: …`) describing the behavior change, one concern per commit.
- Keep module-scope constants upper snake case; match the existing explanatory comment style; Python 3.10+, 4-space indent, type hints on public functions.
