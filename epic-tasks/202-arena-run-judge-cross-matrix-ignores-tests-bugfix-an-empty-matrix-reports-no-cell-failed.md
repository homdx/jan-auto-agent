# 202 — `arena run judge`: the cross matrix only looks in `tests/`, so a round whose tests are in `tests_bugfix/` gets an empty matrix and "no cell failed"

**Status:** queued
**Origin:** judging round 198 (`./arena run judge 198` → "cells: 0 × 7", exit 0), bug 16
**Severity:** MEDIUM (a judging step that reports success having judged nothing; the leads it exists to find are never produced)
**File:** scripts/judge_epic_round.py
**Symbol:** TEST_ROOT, changed_tests, _cross_tests, _render_markdown
**Round:** 202
**Size:** S
**Also touches:** tools/arena/judging.py, tests/test_judge_epic_round_cross.py (or the existing cross test file), tests/test_arena_run_judge.py

## Why

`changed_tests(path, base)` runs `git diff --name-only <base> -- tests/` and keeps files that start with `tests/` (`TEST_ROOT = "tests/"`, line 86). Bug-fix tickets put their tests in `tests_bugfix/` — 198 says so on its `**Also touches:**` line and in its Tests section — and so do 183–187 and 196–201. Round 198: six entries each committed 1–5 files in `tests_bugfix/`; the judge printed

```
Cross — every entry's own tests, run on every implementation (cells: 0 × 7)
Leads — read these first: … none
no cell failed
```

and `cross.json` says `"tests": 0, "cells": 0, "leads": 2`. Exit code 0. Nothing was crossed, and every line of the output says everything is fine. The same wording ("no cell failed", "Failures: None — no cell failed, and none went unrun") is what a quiet, fully-run matrix prints, so a judge reading the end of the output reasonably stops there. (Also: `"leads": 2` while both lists say `none`.)

## Bugs

**16a — only `tests/` is a test root.** `changed_tests` and `TEST_ROOT`: files under `tests_bugfix/` are dropped. (`_xcross_<P>_<file>` copies go to `tests/` of the scratch tree; a bug-fix test is a plain pytest file and runs from there fine — the ticket's own acceptance line is `python3 -m pytest tests_bugfix …`.)

**16b — an empty matrix is reported as a clean one.** A round with 0 providers (no entry changed a test the judge can see) prints the "none"/"no cell failed" verdict. It should say so: a matrix with no cells is "nothing was crossed", exit non-zero (or at least a line at the top and the bottom of the output and in `cross.md`/`SUMMARY.md`), and the `leads` count in `cross.json` must equal the lists printed.

## Fix

- 16a: `TEST_ROOTS = ("tests/", "tests_bugfix/")`; `changed_tests` takes `git diff --name-only <base> -- tests/ tests_bugfix/` and filters on either prefix. A provider file keeps its path in the scratch name (`tests/_xcross_<P>_<file>` for a `tests/x.py`, and `tests_bugfix/_xcross_<P>_<file>` for a `tests_bugfix/x.py`, or encode the root in the name — pick one, but two files with the same basename in the two roots must not collide). Look for other `tests/` literals in `judge_epic_round.py` (line 80 comment, `_cross_cell`'s copy target, the `--tests` flag's "four pytest roots") and in `tools/arena/judging.py`.
- 16b: in `_cross_tests`, `providers == []` → one line, `cross: no entry changed a test under tests/ or tests_bugfix/ — nothing was crossed`, put in `cross.md` and `SUMMARY.md` in place of "no cell failed", and exit code 2 from `judge_epic_round.py --cross`; `arena run judge` passes it through (it already passes exit codes through). `--no-cross` is unaffected. `cross.json["leads"]` is the number of leads in the lists.

## Tests

- A round of two entries whose test files are only in `tests_bugfix/`: the matrix has cells; a `behaviour` lead appears for an entry that breaks the other's test; the columns and rows are the same as for `tests/`.
- Entries with test files in both roots, and with the same basename in both: no collision, both crossed.
- An entry with no test file at all: not a row (as today), still a column.
- Nobody changed a test: exit 2, the "nothing was crossed" line in stdout, `cross.md`, `SUMMARY.md`; `"leads"` is 0 and equals the lists; `arena run judge` returns 2 for it.
- `arena run judge --dry-run` is unchanged.

## Acceptance

```bash
python3 -m pytest tests -k "judge and (cross or run_judge)" -q
python3 -m pytest .smoke_tests/ -q
./arena run judge 198 --no-cross      # unchanged
./arena run judge 198                 # cells > 0, one row per entry that shipped a tests_bugfix file
```
