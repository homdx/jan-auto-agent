# 207 — readers that promise "never an exception" raise: a `NaN`/`Infinity` in context memory and export, a non-UTF-8 byte in `gates`, `arena base check` and `next_task`

**Status:** landed
**Origin:** operator review (`bugs-to-review/`: `test_contest_memory_non_finite.py`, `test_contest_gates_undecodable_git_output.py`, `test_arena_base_check_undecodable_output.py`, `test_next_task_unreadable_progress.py`, bug 212), probed on `arena` @ `00355fd`
**Severity:** LOW (each needs an unusual byte or number, but each ends a long step in a traceback and loses its result: a base check after its minutes, a harvest, a round's start)
**File:** tools/contest/context_memory.py
**Symbol:** _number, load (+ export._number, gates.judge_worktree / gates.git, basecheck.run_steps, scripts/next_task.recorded)
**Round:** 207
**Size:** M
**Also touches:** tools/contest/export.py, tools/contest/gates.py, tools/arena/basecheck.py, scripts/next_task.py, tests_bugfix/

## Why

Five readers say in their docstrings that a bad input is a *row* or an *empty value*, never an exception — and each lets one through, on the two kinds of input a file written by another tool produces: a float that is not finite (`json.loads` accepts the bare words `NaN` and `Infinity`) and a byte that is not UTF-8 (`subprocess.run(..., text=True)` and `open()` decode strictly). Probed on `arena` @ `00355fd`.

## Bugs (each probed)

**35 — `context_memory._number` converts a float to `int` before its `try`** (`context_memory.py:151`).
`int(nan)` raises `ValueError`, `int(inf)` raises `OverflowError`: `cm._number(float('nan'))` → `ValueError: cannot convert float NaN to integer`, `cm._number(float('inf'))` → `OverflowError`. It is called from `OverflowRecord.from_dict`, so one such record makes `context_memory.load` raise — whose docstring says "never an exception, because the only other answer is a round that refused to start for want of a memory file". Fix: the conversion inside the `try`, `except (TypeError, ValueError, OverflowError)` → `None`; and `load` keeps the other records of the file (a test with one bad and two good).

**36 — `export._number` catches `ValueError` but not `OverflowError`** (`tools/contest/export.py:314`).
Docstring: "0 for the malformed — a bad cell is not a bad row". `ex._number(float('inf'))` → `OverflowError` (`nan` already gives `0`). A `state.json` with `"tokens": {"in": Infinity}` kills the export of the whole round. Fix: the same `except` set; a token dict holding `Infinity` still gets its row.

**37 — `gates.judge_worktree` dies on a byte that is not UTF-8 in what git printed** (`gates.py:94`, `:247`, `:480`, `:559`: `capture_output=True, text=True`).
`git show HEAD:<file>` for every file under a `tests/` path (it counts `def test_`), `git show <rev>:tools/auto/collect_bridge.py` for the `_shrink` gate, and `gates.git()` for the log and the diff are all strict text. A committed binary fixture (`tests/fixtures/sample.png`), a Latin-1 byte in a test file, or a Latin-1 commit message is a `UnicodeDecodeError` out of `judge_worktree` — and so out of `harvest`, for an agent whose only offence was a fixture. The scorecard needs the count and the names, never the bytes. Fix: `errors="replace"` (or bytes + a lenient decode) in every one of those reads; the helper `gates.git` keeps everything else exactly.

**38 — `arena base check` ends in a traceback when a step prints a non-UTF-8 byte** (`basecheck.py:287`: `run_steps`, `text=True`; only `OSError` is caught).
A failing test that prints a stray byte, or a `FAILED …` line with a Latin-1 file name, raises `UnicodeDecodeError` out of `subprocess.run` *after* the step ran. It is a `ValueError`, not the `OSError` the step loop catches: the check ends with no table, no verdict and no cache entry, and the `finally` has already removed the worktree. Fix: decode with `errors="replace"` (the summary line may hold a `�`; the verdict never goes missing); stderr too; the later steps still run; "a step that cannot start" is still a failed row.

**39 — `next_task.recorded` lets `UnicodeDecodeError` and `csv.Error` out** (`scripts/next_task.py`).
Probed: a `PROGRESS.csv` starting `\xff\xfe…` → `UnicodeDecodeError: 'utf-8' codec can't decode byte 0xff`; a row with a 200 000-character field → `_csv.Error: field larger than field limit (131072)`. The AR-14 intake check says `run start` "reads the file as if it were empty" for a file that will not decode, and the harvest does the same (`_progress_rows`) — but `recorded`, which both and the coder's own loop read it with, raises. `python3 scripts/next_task.py` prints a traceback instead of the next ticket, on the very command the coder is told to re-run after "a context blow-up mid-round". Fix: `recorded` catches `(UnicodeDecodeError, csv.Error, OSError)` and reads the file as empty (a NUL byte included).

## Tests

One file per bug in `tests_bugfix/` (`test_<area>_<what>_207.py`), each failing on the old code:
35 — `_number` of `nan`/`inf` is `None`, finite floats still truncate, `load` keeps the good records of a file with one non-finite size; 36 — `_number(inf)` and `_number(nan)` are `0`, a token dict with `Infinity` still gets its row; 37 — a binary fixture under `tests/` is a file in the row, a test file with a Latin-1 byte still counts its tests, a Latin-1 commit message is a row, the shrink gate reads a bridge file with a Latin-1 byte, an absent bridge is still `None`; 38 — a red step and a green step with undecodable stdout and stderr are rows (red keeps a summary), later steps still run, a step that cannot start is still a failed row; 39 — an undecodable file, a NUL byte, a huge field are read as empty, the script hands out the ticket, a readable file still skips the recorded ticket.

## Review material

Review tests (held by the operator, outside the repo): `test_contest_memory_non_finite`, `test_contest_gates_undecodable_git_output`, `test_arena_base_check_undecodable_output`, `test_arena_base_check_non_utf8_212`, `test_next_task_unreadable_progress`.
The operator keeps these outside the repo on purpose: a round that can read the reviewer's fix would copy it, and the competition would measure nothing.

## Acceptance

```bash
python3 -m pytest tests_bugfix -k "207" -q
python3 -m pytest .smoke_tests/ -q
python3 -m pytest tests -k "context_memory or export or gates or base_check or next_task" -q
```
