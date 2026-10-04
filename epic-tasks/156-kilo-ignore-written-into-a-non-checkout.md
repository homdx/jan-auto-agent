# 156 — the `.kilo` ignore file is written into a directory that is not a git checkout

**Status:** landed
**Severity:** LOW
**File:** tools/contest/backend.py
**Symbol:** _exclude_kilo_dir
**Round:** 156
**Size:** XS
**Also touches:** tests/test_contest_overflow_wording.py

**Depends on:** 151.

Found while judging round 151: `contest-bench/148/test_bench148.py::test_b3_exclude_on_a_non_checkout` fails on 808cdf8.

## The bug

Round 148's contract: a workspace that is not a git checkout is left alone — nothing is written, nothing deleted, nothing raises. 151 moved the ignore from git's `info/exclude` to `.kilo/.gitignore` in the workspace and writes it everywhere, a plain directory included: `.kilo/` and `.kilo/.gitignore` appear where there is no diff to keep anything out of. The winner also rewrote `test_a_directory_that_is_not_a_checkout_is_left_alone` to expect the file.

## Fix

`_exclude_kilo_dir` asks `git rev-parse --is-inside-work-tree` first (bounded by `_KILO_GIT_TIMEOUT`); anything but `true` returns without a write. The test is back to the 148 contract.

## Tests

`test_a_directory_that_is_not_a_checkout_is_left_alone` and bench 148 `test_b3_exclude_on_a_non_checkout`: `.kilo/` holds only what was there. Both fail on 155.

## Acceptance

```bash
python3 -m pytest tests/test_contest_overflow_wording.py contest-bench/148/test_bench148.py -q
```
