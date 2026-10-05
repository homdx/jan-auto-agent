# 157 — `arena run start/rerun`: the round's lock file is written after the runner child has started

**Status:** open
**Severity:** MEDIUM
**File:** tools/arena/rounds.py
**Symbol:** _run_child
**Round:** 157
**Size:** XS
**Also touches:** tests/test_arena_run_start.py, tests_bugfix/ (a regression guard)

Found as a flake while load-testing arena for round 151 (3 × `pytest tests -n 8` at once, 9 passes): `tests/test_arena_run_start.py::test_the_lock_exists_while_the_child_runs_and_is_gone_after[0]` failed once. 120 reruns of that test under the same load did not repeat it; the cause is in the code, not in the test.

## The bug

`_run_child` (`tools/arena/rounds.py`) does, in this order:

```python
child = SPAWN(line, cwd=str(repo))
...
lock.write_text(f"{child.pid}\n", encoding="utf-8")
```

The child is already running when `.arena/locks/NN.pid` is written. On a loaded box the child can read (or a second `arena run start NN` can check) the lock before it exists. The test's stub child reads the lock first thing, gets `FileNotFoundError`, exits 1, and `run start` returns 1 where 0 was expected. In production the reader is `round_alive` (`rounds.py`, the lock branch): a second `arena run start NN` / `run rerun NN` in that window finds no lock, and — when the child has not yet shown up in `/proc` as `tools.contest run --ticket NN` either — starts a second round on the same ticket. The promise the lock makes, "it exists for as long as the child runs", does not hold for the first moments of the child.

## Fix

- Create the lock **before** `SPAWN`, holding this process's own pid (alive for the whole child run), then replace it atomically (`write` to `NN.pid.tmp` + `os.replace`) with the child's pid once `SPAWN` returns.
- A `SPAWN` that raises removes the lock it created (the `finally` that unlinks today covers only the wait).
- No change to the exit mapping.

## Tests

1. Regression guard (tests_bugfix): a `SPAWN` stub that sleeps 0.5 s after `Popen` before returning, and a child that reads the lock at once — today it fails every time; after the fix the child sees a pid and the lock is gone after.
2. A `SPAWN` that raises `OSError`: refusal, and no `.arena/locks/NN.pid` left behind.
3. The existing `test_the_lock_exists_while_the_child_runs_and_is_gone_after` under 3 × `-n 8` load, 9 passes clean.

## Acceptance

```bash
python3 -m pytest tests/test_arena_run_start.py tests/test_arena_run_rerun.py -q
```

```bash
python3 -m pytest tests_bugfix -n 8 -q
```
