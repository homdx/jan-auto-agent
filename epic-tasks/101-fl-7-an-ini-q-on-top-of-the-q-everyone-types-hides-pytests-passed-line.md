# FL-7 — an ini `-q` on top of the `-q` everyone types is `-qq`, and pytest prints no `N passed` line

**Status:** landed `ec9d78d` (2026-09-24, by hand — the `FL-7:` commit that carries this ticket). Found live the same day in round 74. Two agents, `sensenova-6-8-flash-lite-var1` and `-var2`, ran the full suite three and four times over (2-3 min each) looking for a summary line that pytest never printed. Both ran out of their hour, and both ended `STALLED` with 676 and 782 lines of finished work.
**Severity:** HIGH (the most expensive single fault of round 74. It cost two of ten slots, both holding the best code of the round, and nothing a model can do works around it except guessing that the output is incomplete)
**File:** `pytest.ini`
**Symbol:** `addopts`
**Round:** 101
**Size:** XS
**Source:** `contest-out/74/sensenova-6-8-flash-lite-var2/events.jsonl`, the bash tool calls from 15:53 to 16:04.
**Also touches:** `tests/test_pytest_ini_quiet_summary.py` (new), comments in `tools/auto/executor.py` and `scripts/stress_suite.py` that quoted the old `addopts`

---

## What happened

`pytest.ini` had `addopts = -n auto -q --dist=loadgroup`. Every command in
AGENTS.md, in every ticket's self-check and in what a model writes by habit is
`python3 -m pytest … -q`. Together that is `-qq`, and under `-qq` pytest drops
the final stats line (`5058 passed, 52 skipped in 181s`). What remains is dots,
warnings and `-- Docs: …`.

`sensenova-6-8-flash-lite-var2`, rework attempt, round 74, times are start times:

| time | command | what it saw |
|---|---|---|
| 15:53:29 | `pytest .smoke_tests/ .regression_tests/ -q 2>&1 \| tail -8` | 192 s, the tail of the warnings summary, no result |
| 15:56:46 | `… -q 2>&1 \| grep -E "^[0-9]+ (passed\|failed)\|passed\|failed" \| tail -5` | 167 s, `(no output)` |
| 15:59:45 | `… -q > /tmp/kilo/full.log 2>&1; echo "EXIT=$?"; grep -nE "passed\|failed\|error" …` | 140 s, `EXIT=0` and no `passed` in the log |
| 16:02:10 | `wc -l`, `sed -n … \| cat -A` on the log | looking for the line by hand |
| 16:02:35 | `… -p no:randomly -q --no-header 2>&1 \| grep -E "====="` | 121 s, `(no output)`, then the hour ran out |

That is ten minutes of the rework spent on a suite that had passed on the first
run. `-var1` did the same thing (three runs, about 20 minutes) after a clean
compaction, and was cut off at the hour with no commit.

The harvest had met this before and worked around it, not fixed it:
`tools/contest/gates.py` `_failures` says "under `-qq` — an ini `-q` on top of
a `-q` on the command line — it prints none", and counts `FAILED` lines
instead. The agents have no such workaround.

## The change

- `pytest.ini`: `addopts = -n auto --dist=loadgroup`, with a comment saying why
  there is no `-q`. Now `pytest -q` prints the stats line, and a bare `pytest`
  prints pytest's normal header and progress.
- `tests/test_pytest_ini_quiet_summary.py`: `addopts` holds no `-q`/`--quiet`,
  and a real `pytest -c pytest.ini -n 0 -q` on a one-test file prints
  `1 passed`. Both are red with the old `addopts`.
- Two comments that quoted the old `addopts` are updated.

Nothing that parses pytest's output depends on the ini `-q`. The harvest and
`scripts/stress_suite.py` read the `FAILED` lines, which print at any
verbosity, and `_failures` still handles `-qq` for projects that have it.

## Out of scope

- The prompt. With the root cause gone, the agents need no hint about `tail`.
  KC-53 is where the round prompt names things.
- Other projects' `pytest.ini`. `gates._failures` keeps its `-qq` fallback.
