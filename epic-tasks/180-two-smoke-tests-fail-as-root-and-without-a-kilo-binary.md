# 180 — two smoke tests depend on the operator's box: they fail as root and without `kilo` on PATH

**Status:** landed
**Origin:** `arena-bugs-sonet5` ticket 173 — test-only; passes unchanged on this box (non-root, Kilo installed)
**Severity:** LOW
**File:** tests/test_contest_context_memory.py
**Symbol:** test_a_memory_the_runner_cannot_write_ends_the_overflow_the_way_it_does_today; tests/test_contest_runner_local_store.py: test_the_find_free_line_goes_out_at_intake
**Round:** 180
**Size:** XS
**Also touches:** tests/test_contest_runner_local_store.py, scripts/py_model_test.py

## The bug

Found by running `python3 -m pytest .smoke_tests/ .regression_tests/ tests_bugfix -n 4` in a fresh cloud container (user `root`, no Kilo installed): **9639 passed, 2 failed, 62 skipped** — and both failures are the test's assumption about the box, not the product (ticket 71 "the contest tests stop failing on the operator's own box" and FL-1 set the rule these two break).

1. `test_a_memory_the_runner_cannot_write_ends_the_overflow_the_way_it_does_today` makes the memory folder unwritable with `chmod(0o500)`. **root ignores mode bits**, so the write succeeds, no `context memory` warning is logged, and `assert any("context memory" in record.message …)` fails. The test needs a write that fails for every user: replace the folder by a *file* (`memory.parent.write_text("")` — `mkdir` / `open` under it raises `NotADirectoryError`) or monkeypatch the writer to raise `PermissionError`; `pytest.skip` only if neither is possible.
2. `test_the_find_free_line_goes_out_at_intake` calls `model_check.main()` with `--find-free openrouter`, which reaches `find_kilo(args.kilo)` (`scripts/py_model_test.py:738`) before the stubbed `find_free`; with no `kilo` on PATH and no `~/.vscode/extensions/kilocode.kilo-code-*/bin/kilo` it ends in `SystemExit("kilo not found …")`. The test stubs `find_free` and `_PROC_ROOT` but not `find_kilo`. Add `monkeypatch.setattr(model_check, "find_kilo", lambda explicit: "/bin/kilo")` (the process-count line it asserts does not run Kilo).

## Fix

The two test edits above; no product code changes.

## Tests

Both tests pass as root with no Kilo on PATH **and** as an ordinary user with Kilo installed; the rest of the suite is unchanged.

## Acceptance

```bash
python3 -m pytest .smoke_tests/test_contest_context_memory.py .smoke_tests/test_contest_runner_local_store.py -q
python3 scripts/sync_test_tiers.py --check
```
