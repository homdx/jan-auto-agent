# SLOW-1 — `pytest tests -n 8` got slower: find the commit that did it, and fix it

**Status:** open
**File:** unknown — finding it is the task (tests/, conftest, pytest.ini, test config)
**Symbol:** unknown — the slow tests and whatever schedules them
**Size:** L

## What happens today

At HEAD, `python3 -m pytest tests -n 8` takes roughly 365 s. Some commits
earlier it was under 300 s. HEAD is not the commit that made it slow; the
commits in between are unrelated landings.

## The task

1. Walk the history back (`git log`, `git bisect run` with a timing script,
   timing one file across commits) and name the commit(s) that brought the
   slowdown in — with numbers.
2. Find why: pytest's `--durations=25`, then why each slow test waits. Compare
   the sum of durations with the wall clock — a big gap means something
   serialises the run (an `xdist_group`, a fixture pinning a file to one worker).
3. Fix it without weakening any assertion. A roster default that leaks into a
   test not about it is fixed in the test config, not in the product default.
4. Run `tests`, then `tests_bugfix`, one after the other, never in parallel.

## Acceptance

- The suite is faster than at HEAD; the commit message names the base sha,
  times before and after, and which commit caused each slowdown.
- Both roots green. One concern per commit is welcome.
