# 186 — `test_run_tests_summary_and_tail_agree` fails on a box without the `pytest-timeout` plugin

**Status:** landed
**Origin:** `arena-bugs-opus5`, commit "tests: three contest tests no longer depend on root, kilo or pytest-timeout" (the other two of its three hunks are 180, already landed)
**Severity:** LOW
**File:** tests/test_contest_harvest.py
**Symbol:** test_run_tests_summary_and_tail_agree
**Round:** 186
**Size:** XS

## The bug

The test compares the gate's `run_tests_detail` summary with the old judge module's `run_tests` (`JUDGE_BEFORE`). The old module always passes `--timeout=180` to pytest; without the `pytest-timeout` plugin pytest exits 4 on an unknown option, so `before.run_tests(...)` answers a failure while the gate, which leaves the flag out when the plugin is missing (KC-76), answers `tests:PASS`. The two summaries differ for a reason that has nothing to do with what the test pins ("one summary, one tail").

## Fix

When the plugin is installed the comparison is unchanged. When it is not, the test asserts only what the gate promises there: `summary.startswith("tests:PASS")`.

## Impact here

None today: this box has `pytest-timeout`, so the first branch runs and the test is what it was. It fails in a container that has `pytest` and `pytest-xdist` but not the plugin (the cloud sessions' box). Not reproducible on this box; the change was read, not run on the other branch.

## Acceptance

```bash
python3 -m pytest tests/test_contest_harvest.py -n 4 -q
```
