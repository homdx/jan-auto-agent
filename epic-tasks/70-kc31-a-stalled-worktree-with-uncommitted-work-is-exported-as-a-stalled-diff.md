# KC-31 — a STALLED worktree with uncommitted work is exported as `<agent>.STALLED.diff`

**Status:** queued — after KC-30, last in the queue; found 2026-09-20 in round 60 (KC-21) — the third round in a row to show it. Note added 2026-09-22: round 64 showed it a fourth time, on five agents at once, and made the limit of this ticket plain — a `.diff` on disk is readable by a human but is not an entry, so a round can still report zero while holding work that passes every test root. KC-41 (queued) covers that half: the same worktrees get a deadline commit, a `PROGRESS.csv` row written by the runner, and a real harvest. This ticket keeps the case KC-41 cannot help — a terminal turn whose worktree is genuinely clean — and its `Out of scope` below is superseded accordingly.
**Severity:** MEDIUM (the agent's whole turn is lost when it stalls before `git commit`)
**File:** `tools/contest/cli.py` (`export_patches`), `tools/contest/runner.py` (the terminal branch, for the state)
**Symbol:** `export_patches`
**Round:** 70
**Size:** S
**Source:** KC-21's Status names it — "an uncommitted STALLED worktree should be exported too — `<agent>.STALLED.diff` from `git diff <base_sha>` when `rev-list --count` is 0 and `git status --porcelain` is not empty" — and its *What must change* deliberately left it out, so KC-21 landed without it. Round 60 produced the case again: `mistral-medium-3-5` STALLED at `turn_timeout_sec = 1800` with four modified files in `../rounds/60-mistral-medium-3-5` and no commit; round 58 produced three of them (`contest-bench/kc19/RESULTS.md`), and each time the judge ran `git diff` by hand. A diff is not harvestable — no commit, no claim, no `run.commit` — but it is the operator's to read.
**Depends on:** KC-21 (landed: patches are named by the terminal state), KC-16 (`export_patches`).
**Also touches:** `tests/test_contest_cli.py`

---

## What must change

1. `export_patches` keeps its patch loop, then, for every agent whose state is
   `STALLED` or `ERROR` **without** a `run.commit`, asks the worktree:
   `git rev-list --count <base_sha>..HEAD` is `0` and `git status --porcelain`
   is not empty → write `git diff <base_sha>` (worktree diff, tracked files;
   untracked files are named in a trailing comment, not inlined) to
   `<agent>.STALLED.diff` / `<agent>.ERROR.diff`.
2. A clean worktree with no commit writes nothing, as today.
3. The returned list carries the diffs too, so the run's summary line counts
   them; the exit code is untouched — a diff is not a `READY`.

## Acceptance

- [ ] `tests/test_contest_cli.py`: a `STALLED` agent, zero commits, two edited
      files → `<agent>.STALLED.diff` holds both files' `diff --git` hunks and
      no `From ` header; an `ERROR` one → `.ERROR.diff`; a clean zero-commit
      worktree → no file; an agent with a commit → the KC-21 `.STALLED.patch`
      and no `.diff`.
- [ ] `python3 -m pytest tests -n 4 -q --timeout=180 && python3 -m pytest tests_bugfix -n 4 -q --timeout=180` green (sequentially).

## Out of scope

- Committing on the agent's behalf, or harvesting a diff — KC-22 (landed) is the
  ticket for a turn that stops with uncommitted work while the session is still
  alive, and **KC-41** (queued) is the ticket for the same tree once the session
  is gone and the clock has run out. Superseded note, 2026-09-22: after KC-41
  lands, an agent with a deadline commit exports a `.patch`, and this ticket's
  `.diff` is written only for a terminal turn with a clean worktree.
