# tests_bugfix/

This directory holds every test file whose entire reason for existing is to
pin down one already-fixed, historical bug — the file's own module
docstring says so explicitly (`Bug:`, `BUGFIX`, `AUTO-FIX-N`, `Field
report:`, `Regression test/guard for a bug`, `Before the fix, ...`, and
equivalent framings). These are 107 files, relocated wholesale out of
`tests/` on 2026-09-01 (BUGFIX-SPLIT).

They are **not** feature/acceptance-criteria tests (`AUTO-<X>`, `AUTO-CR-<N>`
"acceptance tests", `COLLECT-<N>`, etc.) — those describe a capability as
originally specified and stay in `tests/`. A file only landed here if its
docstring narrates a defect that shipped, was observed (in a real run, an
audit, or a field report), and was then fixed. See each file's own
docstring for the specific bug it guards against; nothing here has been
rewritten, only relocated (and self-referencing header paths updated).

## Why a separate directory

`tests/` is scanned by `scripts/sync_test_tiers.py` and split into
`.smoke_tests/` (fast) and `.regression_tests/` (heavy) so the routine
pre-commit/pre-push runs stay cheap — see `Tests.MD`. That split is about
*wall-clock cost*, not *what kind* of test it is. This directory is a
third, orthogonal axis: these tests are not part of either tier and are not
discovered by `sync_test_tiers.py` at all (it only looks inside `tests/`),
so moving them here has an immediate, measurable effect on the default
`pytest tests/` / `pytest .smoke_tests .regression_tests` runs — 107 fewer
files, with no risk of a stale-manifest warning from the pre-commit hook,
because `SLOW_TESTS.txt` no longer needs to mention any of them.

The bugs themselves are just as real and just as worth re-verifying before
a release; they are simply not part of the day-to-day loop.

## Running these tests

```bash
python3 -m pytest tests_bugfix -q
```

They are ordinary pytest files — same `conftest.py` bootstrap (project root
is on `sys.path`), same fixtures, same markers (e.g. `xdist_group` on the
`test_llm_stream_*.py` files) as anything in `tests/`, since both
directories are direct children of the repo root.

## What's deliberately unchanged

* No test logic, assertions, or fixtures were touched — only the file's
  location and, where the docstring named its own old path
  (`"""tests/test_x.py — ...`), that one string.
* `test_bug_fix_loop_fuzz.py`, `test_executor_workspace_prune_order.py`,
  and four `test_llm_stream_*.py` files used to be listed in
  `tests/SLOW_TESTS.txt` (heavy tier). They've been removed from that
  manifest — see the note there — since they no longer live under `tests/`
  at all.
* `tests_slow/test_gate1_log_levels_slow.py` split one slow test out of
  `test_gate1_log_levels.py` before this move; its cross-reference comment
  now points at `tests_bugfix/test_gate1_log_levels.py`.
  (2026-09-03: that split existed because `filter()`'s default retry
  backoff cost the test a real ~180s. Once that got a test-only fix —
  same idiom as `tests_bugfix/test_gate1_llm_call_retry_backoff.py`,
  overriding `filt._llm_call_retry_wait_sec` before calling `.filter()` —
  the test no longer needed isolating, so it moved back into
  `test_gate1_log_levels.py` and the now-empty `tests_slow/` was removed.)

## Adding a new file here

Only move a file here if its own docstring already frames it as a
post-hoc regression guard for a bug that shipped — don't backfill this
folder by rewriting a feature test's docstring to sound like a bug report
just to make it qualify.
