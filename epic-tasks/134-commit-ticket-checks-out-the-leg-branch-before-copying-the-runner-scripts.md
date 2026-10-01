# 134 — commit_ticket checks out the leg branch before it copies the runner scripts

**Status:** landed — round 134, winner hy3 (7/7 on contest-bench/134, all 11 entries 7/7, base 2/7), f36dbc7 as-is
**Severity:** MEDIUM
**File:** tools/contest/draft.py
**Symbol:** commit_ticket
**Round:** 134
**Size:** S
**Also touches:** tests/test_contest_draft.py

---

## Why

KC-80: `commit_ticket` copies `CONTEST_SCRIPTS` (`scripts/next_task.py`, `scripts/append_task.py`) into the target **before** it runs `git checkout -q <branch>`. The `if target.exists(): continue` test therefore looks at the operator's current branch, not at the leg branch. Its own docstring states the opposite order ("check out the branch creating it when missing, copy the two scripts … when they are absent").

The failing input:

1. A target repo without the scripts on its own branch (`main`). The first `contest draft --target REPO` succeeds. `contest-legs` does not exist yet, so the `-b` path carries the freshly copied untracked scripts onto the new branch, and they are committed there.
2. The operator runs `git checkout main`, so the scripts are gone from the tree.
3. A second `contest draft --target REPO`: the scripts are absent on `main`, so both are copied in as untracked files. Then `git checkout -q contest-legs` fails with `The following untracked working tree files would be overwritten by checkout: scripts/next_task.py scripts/append_task.py`, and `commit_ticket` returns `CommitResult(False, "cannot check out contest-legs in …")`.

The ticket stays on disk uncommitted, `run --ticket NN --target REPO` cannot start, and two stray script copies are left in the operator's tree. jan-auto-agent itself never shows this, because both scripts are tracked on all its branches. Only `--target` on another repo does.

## What to build

From `tools/contest/draft.py`, `commit_ticket`:

- Move the branch block (`rev-parse --verify --quiet refs/heads/<branch>`, then `checkout -q <branch>` or `checkout -q -b <branch>`, plus the `cannot check out` refusal) up, so it runs right after `_clean_refusal` and **before** the `for rel_script in CONTEST_SCRIPTS:` copy loop.
- The copy loop itself is unchanged: `target.exists()` → skip; missing source → refusal; `OSError` → refusal; a newly written script is appended to `paths`. After the move, `target.exists()` is checked on the leg branch, so a branch that already tracks the scripts copies nothing and commits the ticket alone.
- A failed checkout now leaves no copied script behind, because nothing has been copied yet.
- Nothing else changes: the `add -- *paths` / `commit -q -m subject -- *paths` with explicit paths, the `CommitResult` texts, and the branch left checked out.

## Tests

In `tests/test_contest_draft.py` (fixture `collected`, helpers `_git`, `_git_out`, `_branch`, `_dirty`, `ticket_text`):

- (1) **The bug:** first `commit_ticket(collected, 01-t.md)` → ok, scripts committed on `contest-legs`. Then `git checkout -q <origin branch>` (scripts gone from the tree). Write `02-t.md` and call `commit_ticket` again → `result.ok`, branch is `contest-legs`, and `diff --name-only HEAD~1 HEAD` is exactly `[epic-tasks/02-t.md]` (no scripts re-committed). Before the fix this returns `ok=False` with "cannot check out".
- (2) Same setup, but the leg branch exists and the checkout fails for another reason (e.g. a tracked-on-`contest-legs` file put untracked into the origin tree, or a monkeypatched `_git` failing on `checkout`) → `ok=False`, and neither script was copied into the origin tree (`not (collected / name).exists()` for each of `CONTEST_SCRIPTS`).
- (3) The existing `test_commit_ticket_copies_the_runner_scripts_when_the_target_has_none`, `test_commit_ticket_leaves_a_script_that_is_already_there`, `test_commit_ticket_lands_the_ticket_alone_on_the_leg_branch` and the dirty-repo refusal tests stay green unchanged.

## Acceptance

```bash
python3 -m pytest tests/test_contest_draft.py -n 4 -q
```

```bash
python3 -m pytest tests_bugfix -n 4 -q
```

## Rules

- Tests go into `tests/test_contest_draft.py`. Tier them with `python3 scripts/sync_test_tiers.py` (never by hand), then `python3 scripts/sync_test_tiers.py --check`.
- Keep the heavy explanatory comment style of `tools/contest/draft.py`. Say in a comment why the checkout comes first (the `exists()` check must see the leg branch).
- One commit, subject `134: …`, stating behavior before/after and the test command run.
