# AR-64 — judging a round from arena: `arena run judge NN[.K]`

**Status:** landed
**Severity:** LOW
**File:** tools/arena/judging.py
**Symbol:** run_judge, build_lines, plan_ideal_move
**Round:** 192
**Size:** S
**Also touches:** tools/arena/cli.py, tests/test_arena_run_judge.py, docs/arena/EPIC-ARENA.md

**Depends on:** AR-4 (`run view`), landed. Needs `scripts/judge_epic_round.py --cross` (round 158, 191).

---

## Why

A finished round was judged with two scripts and four paths typed by hand:

```bash
python3 contest-bench/harness/setup_worktrees.py contest-out/184/entrants.json --wt contest-out/184/wt --ideal a0b46f6
python3 scripts/judge_epic_round.py --round 184 --base <sha> --runs contest-out/184/wt --cross --ideal a0b46f6 --jobs 1 --cross-out contest-out/184
```

The base sha comes from `entrants.json`, the folder from the roster's `out_dir`, and `--cross-out`
has to be the leg's folder for a round of legs. Each of these is a place to type it wrong. Round
184 was judged with `--jobs 2`, and on a loaded box the cross matrix filled with `behaviour`
leads that were not there on a quiet one (see "Found while judging round 184").

## What it does

`arena run judge NN[.K] [--ideal REF] [--jobs N] [--cell-timeout SEC] [--no-cross] [--dry-run] [-- judge flags]`

1. Finds the round folder the way `run view` does (`NN`, or a leg `NN.K`).
2. Refuses a round that has no `entrants.json` (exit 1: it has not ended) and a round that is
   still running (exit 2).
3. Reads the base from `entrants.json`. Sets the worktrees up in `<folder>/wt` with
   `setup_worktrees.py`, once: an existing `wt/` is reused.
4. `--ideal REF`: the `ideal` tree is made when it is missing. When it is there at another commit it
   is moved with `git checkout --detach REF`, so one `wt/` can score a candidate and, after the
   candidate is amended, the amended one. (`setup_worktrees.py` skips a tree that exists, which
   would score the second ideal against the first one's checkout and say nothing.)
5. Runs `judge_epic_round.py --round NN --base SHA --runs <folder>/wt --cross --jobs N
   --cross-out <folder> [--ideal REF] [--cell-timeout SEC]` and returns its exit code. A failed setup
   step stops there with its own code.
6. `--no-cross`: the score table alone. `--dry-run`: the command lines, nothing run, nothing written.
   Everything after `--` is appended to the judge's own flags (`--tests`, `--csv FILE`, …).
7. When the box's one-minute load is above its core count, one stderr line says that a cell is
   timing-sensitive. It does not refuse.

`--jobs` defaults to 1: round 184's own tests assert a three second budget, and a cell run beside
another one is the load that breaks them.

## Tests (`tests/test_arena_run_judge.py`)

The minimal command's two lines; every option reaching the judge and the passthrough last;
`--no-cross`; an existing `wt/` reused and a missing `ideal` tree set up; a leg's folder and the
round number; `--dry-run` runs nothing; a bad argument, `--jobs 0`, no `entrants.json`, a running
round and an entrants file without a base each refused in one line; exit codes passed through and
a failed setup stopping the run; the load note, once, and not on a quiet box; `plan_ideal_move`
on a real repository; a second `--ideal` moving the existing tree before the judge starts.

## Found while judging round 184 (recorded, see the bug tickets)

See `193-contest-harvest-group-end-leaves-members-running.md`: a real race in `gates._end_process_group`, found by the matrix and fixed.
