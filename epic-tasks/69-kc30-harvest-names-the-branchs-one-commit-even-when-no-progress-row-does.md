# KC-30 — `harvest` names the branch's one commit even when no `PROGRESS.csv` row does, so every caller stops re-deriving it

**Status:** open
**Severity:** MEDIUM (a finished worktree with no claim row exports no patch at all — the operator never sees the work)
**File:** `tools/contest/harvest.py` (`harvest`, the `HarvestVerdict.commit` it returns)
**Symbol:** `harvest`, `HarvestVerdict`
**Round:** 69
**Size:** S
**Source:** `HarvestVerdict.commit` is only ever the **claim** resolved out of `runs/<agent>/PROGRESS.csv` (`_resolve_claim`); with no row it is `None`, even though `judge_worktree` has already counted the branch's commits and put the one sha in `row["sha"]`. KC-21's own prose says the opposite — "the harvest sets it from the one commit on the branch" — and four of the nine round-60 entries wrote `run.commit = verdict.commit` on that word: an agent that commits its work and stalls **before** `append_task.py` ends with `commit: null` and no `.patch`, which is the failure KC-21 was written to end. The five entries that passed the round's acceptance each carried their own `git rev-parse HEAD` fallback next to the harvest call — the same three lines in five places, and the next caller will forget them.
**Depends on:** KC-14 (`_resolve_claim` takes only a hex sha, landed `720e4d9`), KC-21 (the terminal harvest, landed).
**Also touches:** `tools/contest/runner.py` (the fallback comes out), `tests/test_contest_harvest.py`, `tests/test_contest_runner.py`

---

## What must change

1. `harvest` sets `commit` to the resolved claim when there is one and, when
   there is not, to the branch's **one** commit — `facts["sha"]` /
   `git rev-parse HEAD` when `facts["commits"] == 1`. Two commits or none
   stays `None`: an ambiguous branch must not name a sha.
2. The verdict itself does not move: a tree with no `PROGRESS.csv` row is
   still `REWORK` with `no_progress_row` blocking. Only `commit` changes, so
   `export_patches` has something to format.
3. `run_agent`'s KC-21 branch drops its own fallback and goes back to
   `run.commit = verdict.commit`, one line, as the HARVESTING step does.

## Acceptance

- [ ] `tests/test_contest_harvest.py`: one commit and no row → `REWORK`,
      `reasons == ["no_progress_row"]`, `commit == HEAD`; one commit and a row
      naming it → `commit` is that sha (unchanged); two commits and no row →
      `commit is None`; no commit → `commit is None`.
- [ ] `tests/test_contest_runner.py`: the KC-21 stall-on-a-rejected-commit test
      passes with `run.commit = verdict.commit` and no `rev-parse` in
      `runner.py`.
- [ ] `python3 -m pytest tests -n 4 -q --timeout=180 && python3 -m pytest tests_bugfix -n 4 -q --timeout=180` green (sequentially).

## Out of scope

- The rework message and the gates — a missing row is still the agent's to fix.
