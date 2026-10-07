# 199 — contest and arena small readers and stores: status punctuation, the park `sed`, `retries <= 0`, records from the future, Unicode line breaks in the ini

**Status:** queued
**Origin:** operator review, bugs 1–4, 6, 7 (bug 5, `TicketStore.create`, is already fixed by `bc56155`)
**Severity:** MEDIUM (2, 6), LOW (1, 4, 7), INSIGNIFICANT (3)
**File:** tools/contest/cli.py
**Symbol:** _status_of, _park_line (+ git_run.run_git, models.ModelCache.load, models.ScoreStore.load, models._with_key, context_memory.load, scripts/next_task._status)
**Round:** 199
**Size:** M
**Also touches:** tools/git_run.py, tools/arena/models.py, tools/contest/context_memory.py, scripts/next_task.py, tests_bugfix/

## Bugs (each probed on `HEAD`)

**1 — `_status_of` leaves punctuation on the word** (`contest/cli.py:245`, twin `scripts/next_task.py:63`).
It strips `` `*,;:.- `` only. `queued)` → `queued)`, `(landed)` → `(landed)`, `—` → `—`: a status that `arena issue queue --note` or a hand edit writes is not read as the word. Both readers must stay equal (one shared helper, or a test that pins them to each other).

**2 — `_park_line`'s `sed` does not find the line it parks** (`contest/cli.py:931`).
It builds `s/^\*\*Status:\*\* <lower-cased word>/…/` from the *lower-cased* word with a single space. `**Status:**  Open` (two spaces) and `**Status:** Open` (capital) are not found: the park is a no-op and the commit is empty or refused. `open,` becomes `queued,`. The sed must replace the whole status line by its number, whatever follows `**Status:**`.

**3 — `run_git(retries <= 0)` sleeps on a held `index.lock`** (`git_run.py:107–126`).
The loop is already `range(max(1, retries))`, but the return test still reads `attempt == retries - 1`, which is never true for `retries = 0`, so one wasted `sleep` runs and the retry log prints `retry 1/-1` (probed: `sleeps == [0.25]`). The docstring says `retries=1` is "a single attempt with no waiting"; a value below 1 must be the same.

**4 — `ModelCache.load` and `ScoreStore.load` keep a record from the future for ever** (`models.py:214`, `:796`).
`stamp - at` is negative for an `at` in the future, so the age test passes for as long as the clock does not catch up (probed: a record 10 days ahead loads with a 1-day cache). A clock set wrong once poisons the cache.

**6 — `_with_key` cuts the ini at `U+2028`, `U+0085`, `\x0b`, `\x0c`** (`models.py:518`: `text.splitlines(keepends=True)`).
`configparser` breaks lines on `\n` only, so a value holding one of those is a single line to the reader and two to the writer. Probed: replacing `extra` in `extra = v keep = 2` leaves `keep = 2` behind as a new key; replacing `note` in `note = a\x85b` leaves a bare line `b` and the file no longer loads. Same family as 201 (ticket 196), reached from a hand-written value rather than from `profile set`.

**7 — `context_memory.load` keeps a record from the future for ever** (`context_memory.py:498`: only `record.at < stamp - keep` is dropped).
Same as 4 for `.arena`/contest overflow memory: a future `at` never ages out, and its remembered window size is trusted by every round.

## Fix

- 1: one `strip` set that includes `()[]{}<>—–` and quotes; `next_task._status` imports or copies it, and a test pins the two to each other.
- 2: `sed -i 'Ns/^\*\*Status:\*\*.*$/**Status:** queued/'` by line number (the line is already found by number), or `sed` with a regex that matches the old line as written.
- 3: `retries = max(1, retries)` before the loop, and the loop, the `attempt == retries - 1` test and the log all use it.
- 4 and 7: a record is kept only when `0 <= stamp - at <= days` (a small clock-skew allowance, a named constant, is a reasonable choice and the commit says which). One constant and one helper for the three loaders, not a copy in each module.
- 6: split on `\n` only (`re.split(r"(?<=\n)", text)` or `text.split("\n")` keeping the ends), in `_with_key` and in its helpers.

## Tests

One file per bug in `tests_bugfix/` (`test_<area>_<what>_199.py`), failing on the old code. Cover: 1 — `queued)`, `(landed)`, `—`, `landed,`, `` `open` `` for both readers and that the two agree on a table of ~15 words; 2 — `Open`, `open,`, two spaces, tab, and that the produced command parked on a real temp git repo leaves the file `**Status:** queued` and one commit; 3 — `retries` 0, -1, 1 with a held lock and a `sleep` seam that records calls; 4/7 — a record at `now + 1 s`, `now + 10 days`, `now - days - 1`, exactly at the edge; 6 — ` `, `\x85`, `\x0b`, `\x0c` in an old value and in a neighbouring line, the file loads and holds exactly the keys it had plus the changed one.

## Review material

Review report `bugs-to-review/6.txt`, bugs 1–4, 6, 7 and 16 (a probed repro for each; its test files are held by the operator, outside the repo).
The operator keeps these outside the repo on purpose: a round that can read the reviewer's fix would copy it, and the competition would measure nothing.

## Acceptance

```bash
python3 -m pytest tests_bugfix -k "199" -q
python3 -m pytest .smoke_tests/ -q
```
