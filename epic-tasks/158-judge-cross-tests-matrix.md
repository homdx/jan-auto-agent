# 158 — the judge runs every entry's own tests against every other entry's code and prints the matrix

**Status:** queued
**Severity:** MEDIUM
**File:** scripts/judge_epic_round.py
**Symbol:** (new) cross_tests
**Round:** 158
**Size:** M
**Also touches:** contest-bench/harness/setup_worktrees.py, docs/kilo-contest/RUN-THE-KILO-CONTEST.md, tests/test_judge_cross_tests.py

Idea from judging rounds 145 and 151 by hand: an entry's own tests, run on another entry's implementation, find bugs the judge's bench missed (round 151: the cache that outlived the drop, a linked worktree left with Kilo's own `.gitignore`). Today the judge does it with a shell loop; it should be one command and a table, done at every acceptance.

## What the judge does after a round

1. As today: setup the worktrees, run the bench / the entries' suites, **print the score table first**.
2. Then the cross phase. For every entry P that added or changed test files (from `git diff --name-only <base> -- tests/`), for every implementation I (every entry, plus the base, plus a candidate ideal when given with `--ideal REF`):
   - copy P's new/changed test files into a scratch copy of I under a unique name (`tests/_xcross_<P>_<file>`), never into I's worktree in place;
   - run them with `-n 4`, a private short `--basetemp`, a timeout per cell;
   - delete the copy and the basetemp after the cell (round 151 ran the box out of inodes without that).
3. Print the matrix: rows = whose tests, columns = whose code, cells `passed/total`; under it, per cell, the failing test names with the first `E ` line.
4. Classify each failure, so the operator reads only what matters:
   - `api` — ImportError / AttributeError / TypeError on a name or signature P's code has and I's has not (different implementation, not a bug);
   - `base` — the test also fails on the base (P tests something no one was asked for);
   - `behaviour` — everything else: a candidate bug in I, to be reproduced by hand.
5. Write `contest-out/NN/cross.json` (the raw cells) and `cross.md` (the table), next to `SUMMARY.md`.

## Rules

- Never edits an entry's worktree in place; reads only.
- Sequential cells (the box runs one suite at a time); `--jobs N` opt-in.
- A `behaviour` cell is a lead, not a verdict: the judge reproduces it on the code before a bug is filed, and a reproduced bug gets its own detailed ticket (operator rule, 2026-10-04).

## Tests (offline)

1. Two fake entries in a temp repo, one with a test that passes on both, one with a test using a name only it has: the matrix has the right counts and the second cell is `api`.
2. A test failing on the base too is `base`.
3. A behaviour failure is `behaviour`, with the `E ` line in `cross.md`.
4. No file is left in any worktree and no basetemp is left after the run.

## Acceptance

```bash
python3 -m pytest tests/test_judge_cross_tests.py -q
```

```bash
python3 -m pytest tests -n 8 -q
```
