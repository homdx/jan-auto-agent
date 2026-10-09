# 174 — a ticket or commit subject that is not UTF-8 crashes `arena issue list/view` and `run start` with a traceback

**Status:** landed
**Origin:** `arena-bugs-opus5` ticket 167
**Severity:** MEDIUM
**File:** tools/arena/gitref.py
**Symbol:** git, printable, read_utf8; tickets._text; rounds.find_ticket, run_start
**Round:** 174
**Size:** S
**Also touches:** tools/arena/tickets.py, tools/arena/rounds.py, tests_bugfix/test_arena_non_utf8_ticket_174.py

## The bug

`gitref.git` ran `subprocess.run(..., text=True)` with the locale's strict codec, and the ticket readers used `read_text(encoding="utf-8")`. One byte that is not UTF-8 raised `UnicodeDecodeError`, which no caller catches (they catch `OSError`, `GitRefError`, `RoundError`):

- `arena issue list` / `issue view` / `issue create` (its `next_number` runs `scan`): a latin-1 commit subject anywhere in `git log <branch>` (`_commit_numbers`), or one latin-1 `epic-tasks/*.md` on the branch or in the checkout. **One** such file or commit breaks every listing.
- `arena run start NN`: the same for ticket NN itself, read from the checkout or with `git show`.

Reproduced on a repo with `02-b.md` = `# 2 \xe9t\xe9` and a commit made with `i18n.commitEncoding=latin1`:

```
tickets.scan(...) -> UnicodeDecodeError: 'utf-8' codec can't decode byte 0xe9
```

The module docstring promises "a refusal or a `?` row, never a traceback".

## Fix

- `git()` decodes as UTF-8 with `errors="surrogateescape"`: every byte is kept, so a blob read and fed back as `stdin` (`hash-object` in `build_round_ref`) is byte-identical — the round's base commit holds the ticket exactly as it was.
- `read_utf8(path)` reads a checkout file the same way (`rounds.find_ticket`); the `ticket_sha256` record encodes with `surrogateescape` too.
- `printable(text)` turns the kept bytes into U+FFFD for anything shown: `tickets._text` (titles, `issue view`) and every `GitRefError` message (its stderr line and the echoed command words).
- The lock file `.arena/locks/NN.pid` is read with `errors="replace"`.

## Tests

`tests_bugfix/test_arena_non_utf8_ticket_174.py`:
1. `scan` reads a latin-1 checkout ticket and a latin-1 commit subject: title `2 �t�`, flag `commit 2: on main but status open`.
2. The same when only the branch holds the ticket.
3. `build_round_ref` on a latin-1 ticket: the committed blob is the original bytes with `**Status:** open`.
4. A `GitRefError` whose command holds a bad byte encodes to UTF-8 (no lone surrogate on the screen).
