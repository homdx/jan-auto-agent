# AR-25 — `arena base check`: the base's own checks on a clean checkout, cached per sha

**Status:** queued
**Severity:** LOW
**File:** tools/arena/basecheck.py
**Symbol:** run_base_check, build_steps, run_steps, read_cache, write_cache
**Round:** 194
**Size:** S
**Also touches:** tools/arena/cli.py, tests/test_arena_base_check.py, docs/arena/EPIC-ARENA.md

**Depends on:** AR-1 (the `arena <object> <verb>` skeleton, landed). Nothing else: AR-14 (round 150) is not needed.

---

## Why

A round is only meaningful when its base is green. Today that is checked by hand, at the end of
every round, as four commands typed one after another:

```bash
python3 -m pytest tests -q
python3 -m pytest tests_bugfix -q
python3 scripts/sync_test_tiers.py --check
python3 scripts/check_test_clocks.py --check
```

A base that is already red makes every entry of the next round look broken, and the round is scored
on a failure nobody wrote.

The check has to run on a **clean checkout of the commit**, not on the operator's working tree. On
the operator's box two tests are red in the working tree and green on a clean checkout of the same
commit, because `contest.local.ini` (untracked, never committed) overrides the committed defaults they
read:

* `tests/test_contest_provider_unavailable.py::test_the_committed_default_is_ten`
* `tests/test_contest_roster.py::test_committed_turn_extension_limits_are_a_floor_below_a_ceiling`

An agent works in a clone of the base, so a clean checkout is the base as the agents see it.

## What it does

`arena base check [--ref REF] [--force] [--keep] [--dry-run]`

1. **The commit.** `REF` defaults to the selected profile's `branch`, else `HEAD`. It is resolved to a
   full sha with `git rev-parse --verify REF^{commit}`. A ref that does not resolve, or a directory that
   is not a git checkout, is a refusal: one line `arena: …`, exit 2, nothing created.
2. **The cache.** `.arena/base-check.json` maps a full sha to
   `{"at": <epoch seconds>, "ok": true, "steps": [<the step rows below>]}`. A stored pass for the sha is
   printed with `cached` in its rows and the command exits 0 without running anything. `--force`
   ignores the cache. Only a pass is stored: a red base is run again next time. The file is written
   atomically (a temporary file, then a rename; no `.tmp` is left behind). A file that is missing,
   unreadable or not a JSON object is an empty cache, and is rewritten on the next pass — the cache is
   fail-open, the verdict never is.
3. **A clean checkout.** The sha is checked out into a throwaway detached worktree,
   `.arena/base-check/<first 12 of the sha>`, with `git worktree add --detach`. The operator's working
   tree is never touched, and no untracked or ignored file (`contest.local.ini` included) is copied
   into it. The worktree is removed when the check ends, red or green (`git worktree remove --force`,
   then `git worktree prune`); `--keep` leaves it and prints its path. A tree of the same sha left by
   an earlier crash is removed first.
4. **The steps**, strictly one after another, each its own subprocess with the worktree as its working
   directory. A step that fails does not stop the later ones, so one run names every red step:

   | step | command |
   |---|---|
   | `tests` | `python3 -m pytest tests -q -p no:cacheprovider` |
   | `tests_bugfix` | `python3 -m pytest tests_bugfix -q -p no:cacheprovider` |
   | `tiers` | `python3 scripts/sync_test_tiers.py --check` |
   | `clocks` | `python3 scripts/check_test_clocks.py --check` |

   The repository's own `pytest.ini` decides the parallelism; the check adds no `-n`. `tests` and
   `tests_bugfix` are never run together.
5. **The rows.** Each step is a row `{"step", "ok", "seconds", "summary"}`: `seconds` with one
   decimal; `summary` the last non-empty line of the step's output (`7124 passed in 104.66s`), cut to
   120 characters; for a failed pytest step, up to five of its `FAILED …` lines are appended after it.
   The table (`emit` with those four columns) is followed by one line `base <first 12 of the sha>: ok`
   or `base <first 12 of the sha>: FAILED (<names of the red steps>)`. `-o json` prints the rows
   alone, each carrying `"sha"` and `"cached"`.
6. **Exit codes**, from AR-1's table: `0` every step passed (also a cache hit), `1` at least one step
   failed, `2` a refusal before anything ran (an unresolved ref, not a git checkout, `git worktree
   add` failing). No `3` or `4`.
7. **`--dry-run`** prints the sha, the worktree path and the four commands, one per line, and
   runs nothing: no worktree, no cache file, no subprocess.

The verb is registered in `tools/arena/cli.py` as `OBJECTS["base"]` with the verb `check`, ticket
tag `AR-25`; the logic lives in the new `tools/arena/basecheck.py`.

## Tests (`tests/test_arena_base_check.py`)

Every case runs without the real suites: `build_steps` returns a list the test replaces, or the
step runner is injected, with tiny `python3 -c` commands.

* the four steps, in this order, with the commands above, and the worktree as their working
  directory;
* all steps green: exit 0, the sha in `.arena/base-check.json`; a second run prints `cached`, runs
  no step and exits 0; `--force` runs them again;
* a failing step: the later steps still run, exit 1, the failed test names are in the row, the last
  line names the red step, nothing is cached; a cache entry for another sha is not a hit;
* a cache file that is garbage or empty is an empty cache and is rewritten; no `.tmp` is left;
* an unresolvable ref and a directory that is not a git checkout: exit 2, one stderr line, no
  worktree, no cache file; the default ref is the profile's `branch`, else `HEAD`;
* on a real throwaway git repository: the worktree is made at the sha; a modified tracked file and an
  untracked file in the operator's tree are not visible to a step; the worktree is gone afterwards,
  also after a red run; `--keep` leaves it and names it; a leftover tree of the same sha is replaced;
* `--dry-run` creates no worktree, no cache and runs no subprocess;
* `-o json` is a list of rows with `step`, `ok`, `seconds`, `summary`, `sha` and `cached`; the table
  has the four columns and the verdict line;
* a bad argument (`--ref` with no value) is exit 2 and one line.

## Not in this ticket

Running `base check` from `run start` (AR-31), comparing a step's seconds with an earlier run (AR-51),
`--stress N`, and the `.collect/` freshness check (AR-10).

## Acceptance

Run each command separately and in this order:

```bash
python3 scripts/sync_test_tiers.py
```

```bash
python3 scripts/sync_test_tiers.py --check
```

```bash
python3 -m pytest tests/test_arena_base_check.py -q
```

```bash
python3 -m pytest tests -n 8 -q
```

```bash
python3 -m pytest tests_bugfix -n 8 -q
```

All cases above pass.

---

## Rules

- The new logic is in `tools/arena/basecheck.py`; `tools/arena/cli.py` gets the registration and the
  handler only. **No change under `tools/contest/` and nothing under `scripts/`** (the two scripts the
  steps call are used as they are).
- `REPO_ROOT` (`tools/arena/cli.py`) is the only repo seam; never read the current working directory.
- **No real suite, no network and no real round in the tests.** Every step command is a tiny
  `python3 -c` replacement; the git cases run on a throwaway repository in `tmp_path`.
- **Never run `arena base check` without `--dry-run` while working on this ticket**: it would start
  the whole test suite inside the suite. `--dry-run` is safe. Never run `arena run start|rerun …` or
  `python3 -m tools.contest run …`.
- Every refusal is one line through `output.refuse`, exit 2, and writes nothing.
- The cache is written only to `.arena/base-check.json`, and a worktree only under
  `.arena/base-check/`; nothing else in the repository is created or changed by the command.
- Do not change existing tests except to add cases. Never print an api key or a URL with credentials.
- Python 3.10+, four-space indent, type hints on public functions, explanatory comments in the style
  of `tools/arena/judging.py`.
- Tests wait on events, never on sleeps or tight timeouts.
- No unrelated repository changes.
- Commit subject starts with the round number: `194: …`.
