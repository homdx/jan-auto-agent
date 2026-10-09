# 181 — arena run: a failed lock swap after spawn orphans the runner

**Status:** landed
**Origin:** `arena-bugs-opus5` ticket 170 (the commit after its first push) — its fix as written broke the 157 guard, see Landed
**Severity:** LOW
**File:** tools/arena/rounds.py
**Symbol:** _run_child
**Round:** 181
**Size:** XS
**Also touches:** tests_bugfix/test_arena_lock_swap_failure_181.py

## Bug
After the arena merge (lock written before the child starts), one `try` wrapped
both `SPAWN(...)` and the swap of the lock to the child's pid
(`lock_tmp.write_text` + `os.replace`). An `OSError` from the swap (disk full,
read-only `.arena/`) landed in the `except OSError` meant for spawn failures:

- arena printed `cannot start the runner: …` although the runner **was** running;
- arena returned at once without waiting the child, and deleted the lock;
- the round then ran unlocked: a second `arena run NN` could start a second
  runner on the same round; `NN.pid.tmp` was left behind.

## Fix
Spawn (and the first lock write) keep their own `try` → `cannot start`.
After the spawn, a failed swap is tolerated: the tmp file is removed, arena's
own pid stays in the lock, and the child is waited out as normal.

## Tests
`tests_bugfix/test_arena_lock_swap_failure_181.py` — the swap failure case
fails on the old code (child never waited, exit "cannot start"); the spawn
failure still refuses and leaves no lock.

**Landed:** the bug is real on the 157 code (reproduced: with `os.replace` raising ENOSPC, arena printed
`cannot start the runner: [Errno 28]`, returned without `wait()` and removed the lock, runner still up).
Opus 5's patch as pushed splits SPAWN into a `try` that catches only `OSError`, and the unlink-in-`finally`
sits on the second `try` only, so a `RuntimeError` from SPAWN left the lock behind — it fails
`contest-bench/157/acceptance_157.py::test_a_spawn_that_raises_anything_leaves_no_lock` (round 157's decider).
Landed form: ONE outer `try/finally` (lock gone on anything), an inner `except OSError` around lock-write +
SPAWN (`cannot start the runner`), and a tolerated swap failure after the spawn. A third test pins the
`RuntimeError` case next to Opus 5's two.
