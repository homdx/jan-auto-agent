# FL-2 — the `index.lock` retry landed in `GitManager` only: every other git call in the tree still exits 128 on a lock another git is holding

**Status:** queued — after FL-1 (84, landed `e500d40`). FL-1 fixed the caller that the stress run happened to hit; the same failure mode is live in five more modules, and in a multi-agent round they are the ones running git concurrently.

**Severity:** HIGH — a round runs `worktree add` / `checkout -B` / `clean -fdx` / `status` for N agents against one repo at the same time, and a single collision loses an agent's workspace or misreads its tree as clean.
**File:** `tools/contest/workspace.py` (`_git`), `tools/contest/gates.py` (`git`), `tools/contest/runner.py` (`_dirty_paths`), `tools/collect/manifest.py` (`_git`), `tools/contest/cli.py` (the `format-patch` call), `tools/auto/delta_validator.py`.
**Symbol:** `workspace._git`, `gates.git`, `runner._dirty_paths`, `manifest._git`, `GitManager._run`, `GitManager._LOCK_CONTENTION_RE`.
**Round:** 85
**Size:** S
**Source:** `e500d40` (FL-1) added `_LOCK_CONTENTION_RE` / `_LOCK_RETRIES` / `_LOCK_BACKOFF_S` to `tools/auto/git_manager.py:419` and split `_run` / `_run_once`. Nothing outside that class knows about it. Grep of the tree at `d8e6d1a`:

```
tools/contest/workspace.py:112   ["git", "-C", str(cwd), *args]      # worktree add, checkout -B, clean -fdx, status
tools/contest/gates.py:56        ["git", *args]
tools/contest/runner.py:410      ["git", "status", "--porcelain", "--untracked-files=all"]
tools/collect/manifest.py:153    ["git", "-c", "core.quotepath=false", *args]
tools/contest/cli.py:576         ["git", "-C", …, "format-patch", …]
tools/auto/delta_validator.py:105 ["git", "-C", …, "show", …]
```

`git status` is not a read: it refreshes and rewrites the index, so it takes `.git/index.lock` like `add` does. `checkout -B`, `clean -fdx` and `worktree add` all write it. `show`, `rev-list`, `rev-parse`, `log`, `merge-base` and `format-patch` do not.
**Depends on:** FL-1 (84) — this reuses its regex and its `_run`/`_run_once` split, so it must land after it.
**Also touches:** `tests/test_contest_workspace.py`, `tests_bugfix/test_git_manager_has_staged_changes_oserror.py` (shared helper), plus a new test file for the shared runner.

---

## What happens today

git holds `.git/index.lock` for the whole of any index-writing command, and a second git that finds it there does not wait — it exits 128 with `Unable to create '<path>/.git/index.lock': File exists`. FL-1 established that this is a **transient**: the holder is a moment from releasing it, and the right answer is a short bounded retry, because a *stale* lock left by a crashed git never clears and must still surface the same error.

`GitManager._run` now does exactly that: 8 attempts, 0.25 s apart, only on that one signature, with an ordinary git failure raised on the first attempt. Every other git caller in the repo was written before that and still takes the first 128 as final.

That matters most in the one place the project actually runs git concurrently. `workspace.py` prepares one worktree per agent, and `prepare_workspaces` runs them against **one** repository: `worktree prune`, `worktree add`, `checkout -B`, `clean -fdx -e runs/`. A round of eight agents is eight of those sequences racing each other plus whatever the agents' own sessions are doing inside their trees. `WorkspaceError` from a collision is not retried anywhere up the stack — the agent loses its workspace before the round starts, and the round reports it as a setup failure with a git error string that reads like a real problem.

`runner._dirty_paths` is the second one and it fails *silently*, which is worse:

```python
out = r.stdout if r.returncode == 0 else ""
```

A `git status` that lost the lock race returns 128, the function reads that as **no dirty paths**, and the turn is scored as a clean tree. On the KC-31/KC-41 path that is the difference between harvesting an agent's work and reporting zero entries.

`gates.git` defaults to `check=False` and returns stripped stdout, so a lock collision there is an empty string — an empty diff, an empty file list, a gate that passes because it saw nothing.

## What must change

One shared retry, used by every caller that can write the index; not six copies of the ladder.

1. Lift the retry out of `GitManager` into one place both sides can import — the regex, the attempt count, the backoff, and a `run_git(cmd, cwd, …)` that wraps `subprocess.run` and repeats only on that signature. `GitManager._run` keeps its `GitError` wrapping and delegates; `_run_once` stays as the single attempt.
2. Route `workspace._git`, `gates.git`, `runner._dirty_paths`, `manifest._git` through it. Read-only subcommands may go through it too — the regex simply never matches — so no caller needs a whitelist of which git writes the index.
3. `runner._dirty_paths` must stop reporting a failed `git status` as a clean tree. After the retries are exhausted it is a *read error*, not a clean tree: raise, or return a sentinel the caller can tell apart from `""`. Deciding "no uncommitted work" from a command that did not run is the actual bug here; the lock is only the common way to trigger it.
4. Same call, same bound: 8 attempts 0.25 s apart, so a stale lock costs two seconds and then raises the original error, which already names stale locks as the likely cause.

## Acceptance

- [ ] One module owns the lock regex, the retry count, the backoff and the runner; `grep -rn "index.lock\|File exists" tools/` finds the pattern in exactly one place.
- [ ] `GitManager` behaviour is unchanged — the three tests that shipped with `e500d40` still pass untouched, and the first of them still fails if the retry is removed.
- [ ] A test holds `.git/index.lock` for ~0.5 s from another thread and asserts that a `workspace._git` `checkout -B`, a `runner._dirty_paths` and a `gates.git` call all still succeed; the same test with the lock held for the whole ladder asserts each one raises (or reports a read error) with the original git text, not a timeout.
- [ ] `runner._dirty_paths` on a `git status` that exits non-zero does **not** return `""` as "clean" — the caller can distinguish "tree is clean" from "could not read the tree", and a test proves it.
- [ ] An ordinary git failure (bad ref, not a repository) is raised on the first attempt — no test that asserts a git error takes two seconds longer than it used to.
- [ ] `pytest tests -n 4 -q --timeout=180` then `pytest tests_bugfix -n 4 -q --timeout=180`, sequentially, both green; the FL-1 stress command (four suites, 64 workers) green.
- [ ] `python3 scripts/sync_test_tiers.py --check` clean.

## Out of scope

- Any change to what the callers *do* with git — no new subcommands, no new flags, no change to `prepare_workspaces`' worktree layout.
- Serializing the round's git calls behind a process-wide lock. The retry is per-call and bounded; a global lock would hide a real deadlock and slow every read.
- Waiting on a stale lock, or deleting `.git/index.lock` on the caller's behalf. Never delete it: the ladder ends by raising the error that already explains stale locks.
- `tools/auto/collect_bridge.py`. `CollectBridge._shrink` stays byte-identical.

## Self-check before `append_task.py` (required — every item, in the worktree you submit)

- [ ] `python3 --version`; no backslash and no nested same-quote inside an f-string expression.
- [ ] Exactly one commit on top of the base; only this ticket's work.
- [ ] `git diff --stat <base>..HEAD` names only the files listed above plus the new test file and its `.smoke_tests/` link. Never `epic-tasks/`.
- [ ] The held-lock test fails without the change, on every caller it covers.
- [ ] Both test roots green, sequentially; the FL-1 stress command green.
- [ ] `CollectBridge._shrink` byte-identical.
- [ ] `scripts/append_task.py` from the worktree with the **sha** of the one commit — not `HEAD`; hand in `git format-patch <base>..HEAD`.

## Ground rules (same as every round)

- Do not touch `CollectBridge._shrink`.
- No test starts a real `kilo` or calls a live provider.
- Do not edit `epic-tasks/`.
- One commit, no push; a test ships with the change and fails without it.
