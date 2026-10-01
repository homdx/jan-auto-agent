# KC-23 — `prepare_round` refuses to reset a round worktree that carries work unless `--fresh`; the message names `--resume`

**Status:** landed `2a7e899` (round 62, 4 live entries) — round 62 of EPIC KC (`docs/kilo-contest/EPIC-KC.md`); found 2026-09-20 reading `workspace.reset_worktree` after round 52's three manager runs, each of which started by resetting `../rounds/52-*` — twice over trees that held a finished commit (`hy3 e71f221`, `mistral 9f05e3c`) the operator had exported by hand first. Independent of KC-19/21/22 (different file).
**Severity:** MEDIUM
**File:** `tools/contest/workspace.py` (`reset_worktree`, `prepare_round`), `tools/contest/cli.py` (`run` — the `--fresh` flag)
**Symbol:** `prepare_round`, `reset_worktree`, `WorkspaceError`
**Round:** 62
**Size:** S
**Source:** `reset_worktree` does `git reset --hard <base>` + `git clean -fdx -e runs/` on every roster worktree, unconditionally. A clone (`ensure_clone`) already refuses when dirty unless `--force-clone`; a worktree does not. The one way to keep the work is to remember `--resume` — and `--resume` is only right when `state.json` is from the same round. An operator whose round died (a crashed `kilo serve`, a closed laptop, Ctrl-C) and who types the command again — the natural thing — loses every uncommitted edit and every commit on `contest/NN/<agent>` without a word.
**Depends on:** KC-4 (`workspace.py`, landed `5cf7eef`), KC-16 (`cli.py run`, landed `1304950`).
**Also touches:** `tests/test_contest_workspace.py`, `tests/test_contest_cli.py`

---

## What must change

1. `reset_worktree(..., *, force: bool = False)`: before the reset, when the
   worktree exists, compute `commits = git rev-list --count <base_sha>..HEAD`
   (0 when the branch is not above the base, e.g. a new base) and
   `dirty = git status --porcelain --untracked-files=all` minus `runs/`
   lines. If `commits > 0` or `dirty` and not `force` → raise
   `WorkspaceError` naming the worktree, the count and the first few dirty
   files, and ending with: `pass --fresh to discard it, or --resume to
   continue the round from contest-out/<NN>/state.json`. A worktree that
   is clean at the base, or does not exist yet, resets as today.
2. `prepare_round(..., *, force: bool = False)` threads it through;
   `cli.py run` grows `--fresh` (store_true, help "reset the round's
   worktrees even when they hold uncommitted work or commits") and passes
   it; `--resume` never resets (unchanged). The intake plan lines gain no
   row.
3. The error surfaces the way every other `WorkspaceError` does in `run`:
   `intake: <message>` on stderr, `EXIT_FAILED`, no server started.

## Acceptance

- [ ] `tests/test_contest_workspace.py`: a prepared worktree with one
      commit above the base → `prepare_round` raises `WorkspaceError`
      whose text contains the worktree path, `1 commit`, `--fresh` and
      `--resume`; the same with an uncommitted edit and no commit → raises,
      names the file; `force=True` → resets, the file is gone, `HEAD` is
      the base; a clean worktree at the base → resets silently as today;
      a worktree from a **previous** base (branch behind the new base, no
      commits above it, clean) → resets silently.
- [ ] `tests/test_contest_cli.py`: `run` on a sandbox whose worktree holds
      a commit → `intake:` line on stderr with `--fresh`, exit `EXIT_FAILED`,
      no `kilo serve` spawned (the `spawn_holder` fixture saw nothing);
      `run --fresh` on the same sandbox → proceeds (worktree reset).
- [ ] Every existing test in both files stays green, and only
      `test_prepare_round_is_idempotent_after_progress_and_commits` changes —
      its rerun now passes `force=True`, because a silent reset over a stray
      commit and `PROGRESS.csv` is exactly the behaviour this ticket removes;
      asserting the old silent reset would contradict the fix. `remove_round`
      and `ensure_clone` are untouched.
- [ ] `python3 -m pytest tests -n 4 -q --timeout=180 && python3 -m pytest tests_bugfix -n 4 -q --timeout=180` green (sequentially).

## Out of scope

- Auto-detecting that `state.json` belongs to this round and switching to
  `--resume` on its own — the operator decides.
- Exporting the work before discarding it under `--fresh` — the operator
  has `git -C ../rounds/NN-<agent> format-patch <base>..HEAD` (and KC-21
  exports terminal states).

## Self-check before `append_task.py` (required — every item, in the worktree you submit)

- [ ] `python3 --version` on the judge is **3.10.12**;
      `python3 -c "import tools.contest.workspace, tools.contest.cli"` from
      the repo root. No backslash and no nested same-quote inside an
      f-string expression.
- [ ] Exactly **one** commit on top of the base; only this ticket's work.
- [ ] `git diff --stat <base>..HEAD` names only `tools/contest/workspace.py`,
      `tools/contest/cli.py` and the two test files (plus `.smoke_tests/`
      links). Never `epic-tasks/`.
- [ ] `python3 scripts/sync_test_tiers.py --check` is clean.
- [ ] The new tests are red without the change.
- [ ] `python3 -m pytest tests -n 4 -q --timeout=180` then
      `python3 -m pytest tests_bugfix -n 4 -q --timeout=180`, **sequentially**,
      both green.
- [ ] `CollectBridge._shrink` byte-identical.
- [ ] `scripts/append_task.py` from the worktree with the **sha** of the
      one commit — not `HEAD`; hand in `git format-patch <base>..HEAD`.

## Ground rules (same as every round)

- Do not touch `CollectBridge._shrink`.
- No test starts a real `kilo` or calls a live provider.
- Do not edit `epic-tasks/`.
- One commit, no push; a test ships with the change and fails without it.
