# 191 — judge `--cross`: an `--ideal` (or `--base`) ref the entries' checkouts lack is dropped without a word

**Status:** landed
**Severity:** MEDIUM
**File:** scripts/judge_epic_round.py
**Symbol:** _cross_tests, _render_markdown
**Round:** 191
**Size:** XS
**Also touches:** tests/test_judge_cross_tests.py

Found while judging round 146 (2026-10-06). The candidate ideal was committed in the
judge's own checkout, as `refs/judge146/ideal`. The round's entries were separate
clones in `../rounds/146-*`. The first `--cross` run printed `cells: 9 × 13`, with no
ideal column and no message. The matrix looked complete, but the ideal was never
scored.

## The bug

`_cross_tests` builds the `base` and `ideal` columns only when
`_resolve(repos, ref)` finds the ref. `repos` is the entries' checkouts and nothing
else. A ref that exists only where the judge runs (`REPO_ROOT`) resolves in none of
them, and both `if anchor:` and `if ideal_repo:` then skip the column silently.
`cross.md` still shows `- ideal: —` or the ref, so nothing on the screen or in the
artifacts says that the column is missing.

## How to reproduce (offline)

1. Make a repo `main` and clone it to `entry`.
2. Commit a change and a test in `entry`.
3. Only after that, commit an `ideal` branch in `main`, so the clone does not have it.

```bash
python3 scripts/judge_epic_round.py --round 1 --base <main sha> --worktree e=<entry> \
    --cross --ideal <ideal sha>
```

On arena 76549b5 this prints `cells: 1 × 2` (`e` and `base`, no `ideal`), with exit 0
and no line about the ideal.

## Fix

- A ref is looked up in the entries' checkouts first, then in `REPO_ROOT`. The order
  matters: when `--base competition` names a branch, the entries' branch must win over
  a different branch of the same name in the judge's checkout.
- A ref found nowhere gets one line:
  `cross: --ideal REF resolves in no checkout (the entries' or <REPO_ROOT>) — no ideal column`.
  The same applies to `--base`.
- `cross.json` gains `"skipped": {column: ref}`.
- In `cross.md`, the `- ideal:` / `- base:` line ends with
  `— resolves in no checkout, no column`.
- A `base` or `ideal` worktree that is named on the command line is still that column,
  as before.

## Tests (`tests/test_judge_cross_tests.py`, appended)

1. An ideal that only the judge's checkout has (`REPO_ROOT` patched to a second repo)
   is a column, and its cell is scored.
2. A ref found nowhere produces the exact line, `skipped` in the JSON and the note in
   `cross.md`.
3. A branch name that resolves in both places takes the entries' commit.

On the old code, 1 and 2 fail. Each part of the fix was mutated away in turn and each
mutation failed a test:

- no `REPO_ROOT` fallback;
- no line;
- no `skipped`;
- no `cross.md` note;
- `REPO_ROOT` tried first (caught by test 3).

Real data: round 146 with mimo's clone and the ideal only in this checkout now prints
`cells: 1 × 3`; mimo's tests pass 107/110 on the ideal.

## Seen while checking, not in this ticket

A cell whose code under test starts the real `kilo` binary can leave an empty
`/tmp/judgecell-*/.cell_tmp/kilo` behind. This happened twice in round 146's run, both
in timed-out cells on sensenova-6-7-flash-lite-var1's code. The `kilo` child outlives
the process-group kill and re-creates the directory after the scratch is removed; no
process was left running. This needs a ticket of its own.

## Acceptance

```bash
python3 -m pytest tests/test_judge_cross_tests.py -q
```

```bash
python3 -m pytest tests -n 8 -q
```
