# 200 — arena tickets, output and basecheck: `set_status_text` offsets and line endings, the refusal block's columns, the summary line, the shared cache temp file

**Status:** landed
**Origin:** operator review of AR-14 (150) and AR-25 (194), bugs 12–15 and two findings
**Severity:** MEDIUM (12: a ticket loses a line), LOW (13, 14, 15, summary, cache temp)
**File:** tools/arena/tickets.py
**Symbol:** set_status_text, _CLOSED_LINE_RE (+ output._wrap, output.refuse_ctx, output._hint_line, basecheck._summary, basecheck.write_cache)
**Round:** 200
**Size:** M
**Also touches:** tools/arena/output.py, tools/arena/basecheck.py, tests_bugfix/

## Bugs (12–14 probed on `HEAD`)

**12 — a `**Closed:**` line above `**Status:**` makes `set_status_text` cut the wrong text** (`tickets.py:694–710`).
The status line is found once, in the original text; then the `**Closed:**` line is removed from `text`, and the old offsets are applied to the shortened string. Probed: `# T⏎**Closed:** why⏎**Status:** closed⏎**File:** f⏎` with `open` → `# T⏎**Status:** clos**Status:** open` — the `**File:**` line is gone. The ticket loses data silently.

**13 — `close → reopen` does not give the ticket back byte for byte** (`tickets.py:645, 701`).
`_CLOSED_LINE_RE` already ends in `\r?\n?`, so the removal code at `:701` takes a second `\n` and eats the blank line after `**Closed:**`. Probed: `# T⏎**Status:** open⏎⏎body⏎` closed with a reason and reopened is `# T⏎**Status:** open⏎body⏎`. The docstring promises "everything else byte for byte".

**14 — a CRLF ticket gets mixed line endings** (`tickets.py:704–710`).
The new status line is written without the original `\r`, and `**Closed:**` is added with a bare `\n`. Probed: `**Status:** closed⏎**Closed:** r⏎` inside a `\r\n` file.

**15 — the refusal block's labels and wrapping** (`output.py:159–207`).
`_wrap` formats `f"  {label}: "` and `refuse_ctx` passes `"where:"`, `"ticket:"`: the output is `where::` and `ticket::`, and the two labels are not padded to the width of `flow:` — spec 150 §7 draws `  where:  `, `  ticket: `, `  flow:   `, values in one column. A `flow` detail, a hint's `why` or `command` that holds a newline breaks the "one line" of the block (`_hint_line` only scrubs; `flow_line` does not fold).

**Summary line** (`basecheck.py:251–268`). `_summary` takes the last non-empty line of `stdout` + `\n` + `stderr`, so a step that prints warnings on stderr after the pytest summary shows a warning, not `N passed in …s`. Take the last line of stdout; stderr only when stdout is empty.

**Shared temp file** (`basecheck.py:195–214`). `write_cache` writes `contest-cache.json.tmp` — one fixed name for every process. Two `arena base check` runs at once can interleave their writes (a corrupt cache) or lose the file between `write_text` and `os.replace` (one returns `False`). Use `tempfile.mkstemp` in the same folder, like `models.write_profile_keys`.

Not in scope, a decision for the operator: `basecheck._hit` accepts a cached pass with fewer steps than `STEPS`; `tests/test_arena_base_check.py` feeds it two steps on purpose. Do not change it.

## Fix

- 12–14: one pass over lines. Find the status line and the `**Closed:**` line in the same text, take the line ending from the status line (`\r\n` or `\n`), write the status line and, for `closed`, the `**Closed:**` line with that ending, and drop an old `**Closed:**` including its own ending only. Nothing else is touched; `close → reopen` is the identity.
- 15: labels padded to one width (`where:  `, `ticket: `, `flow:   `), values aligned, `_wrap` without its extra colon; every newline in `detail`, `why`, `command`, `message` folded to a space (`_one_line`) before printing and in the JSON.
- `_summary`, `write_cache`: as above.

## Tests

`tests_bugfix/test_arena_set_status_text_200.py` (12, 13, 14: Closed above and below Status, no Closed, a reason with a trailing space, CRLF and LF, several close/reopen cycles are the identity, a missing status still raises `ValueError`) and `tests_bugfix/test_arena_refusal_block_200.py` (15: the exact text for the §7 example, a multi-line `command`, `-o json` unchanged except for folded newlines), plus one each for `_summary` (stderr warning after pytest's line) and `write_cache` (two threads writing at once leave a valid file and no `.tmp`).

## Second review (cross-check on `arena` @ `00355fd`)

An independent review (`bugs-to-review/`, `test_arena_set_status_text_round_trip` and `…_set_status_text_210`) re-found 12–14 and its regression tests still fail on this tree (11 round-trip cases, the CRLF `close`/`reopen`/plain-change cases, the `**Closed:**`-above-`**Status:**` case). Two extra cases worth pinning: a `**Closed:**` line that is the *last* line takes its own newline with it, and `reopen` after a note keeps the note out but moves nothing else.

## Review material

Review tests (held by the operator, outside the repo): `test_arena_set_status_text_round_trip` (11 tests, 15 failing cases here), `test_arena_set_status_text_210`; report `bugs-to-review/6.txt`, bugs 12–15.
The operator keeps these outside the repo on purpose: a round that can read the reviewer's fix would copy it, and the competition would measure nothing.

## Acceptance

```bash
python3 -m pytest tests_bugfix -k "200" -q
python3 contest-bench/150/acceptance_150.py
python3 -m pytest .smoke_tests/ -q
```
