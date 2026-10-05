# 184 — `test_run_tests_detail_ends_the_suite_at_the_harvest_budget` needs a nested pytest to reach its slow test inside 3.0 s: it fails when the box is busy

**Status:** queued
**Severity:** MEDIUM
**File:** tests/test_contest_harvest.py
**Symbol:** test_run_tests_detail_ends_the_suite_at_the_harvest_budget
**Round:** 184
**Size:** XS
**Also touches:** tools/contest/gates.py (only if the budget's meaning has to change, see the options)

Found while porting the cloud bug branches (2026-10-05): a full `tests -n 4` run under `nice -n 19` next to the live round 158 (load average 10–14) failed this test once; the same test, run alone on the **base** tree at the same load, failed 1 of 2. Nothing in the change under test was involved. The standard load run (3 × `tests -n 8` at once on an idle box) has never shown it: it needs the *test's own* process to be starved, not the box to be busy.

## The bug

The test builds a worktree whose suite holds one test that sleeps for 90 s, calls

```python
summary, tail = run_tests_detail(str(wt.path), budget_sec=3.0)
```

and then asserts, among other things, that the tail **names where the budget hit**:

```python
assert any("the budget hit in:" in line and "test_slow_kc57.py::test_slow_kc57" in line for line in tail)
```

`budget_sec` is a wall clock that starts with the nested `pytest`'s process. The line can only name `test_slow_kc57` if the nested pytest has got through interpreter start, plugin load, the worktree's `conftest.py` and collection and **has started the slow test** before the 3.0 s are up. On a quiet box that takes about 1.5 s. When the process gets a fraction of a core the budget is spent in collection, the summary is still `tests:budget✗` (the first assertions pass) and the tail names no test — the last assertion fails with `assert False`.

So the assertion tests the machine's speed, not the rule "a root that outlives `budget_sec` is ended by its process group and the tail names where it was". Every other assertion of the test holds under load.

## How to reproduce (offline)

Sixteen busy loops on an eight-core box, the test at `nice -n 19`:

```bash
for i in $(seq 1 16); do python3 -c "import time; e=time.time()+100
while time.time()<e: pass" & done
nice -n 19 python3 -m pytest tests/test_contest_harvest.py::test_run_tests_detail_ends_the_suite_at_the_harvest_budget -n 0 -q
```

Result on arena at ae20069: 1 failed, 3 passed in four runs (`assert False` on the `the budget hit in:` line). The same loops with the test at normal priority: 4 passed in 4; no loops: passes.

## What to change

Make the assertion independent of how fast the nested pytest starts. Any of these, the competition may pick:

1. The slow test writes a marker file when it starts; the test waits for it (up to 60 s) with `budget_sec` large enough that the marker is certain to appear first, and asserts the tail only after that. The budget stays a budget, the start-up time is out of the equation.
2. Start the budget clock when the first test starts: not for this ticket unless `run_tests_detail` already knows the moment (it does not).
3. Keep 3.0 s, but have the worktree's suite import nothing (no repo `conftest.py`) so the start-up is short whatever the load, and make the *name* assertion conditional on the marker of option 1.

Do not weaken the other assertions: `tests:budget✗` in the summary, `took < 15`, the group ended once, no live pytest left.

## Tests

1. The test passes 20 times in a row under the reproduction above.
2. The test still fails if `run_tests_detail` stops ending the group at the budget (mutate `_end_process_group` to a no-op: the `os.killpg(ended[0], 0)` assertion fires).
3. Every other test in `tests/test_contest_harvest.py` is unchanged and green.

## Acceptance

```bash
python3 -m pytest tests/test_contest_harvest.py -n 4 -q
```

```bash
python3 -m pytest tests -n 8 -q
```
