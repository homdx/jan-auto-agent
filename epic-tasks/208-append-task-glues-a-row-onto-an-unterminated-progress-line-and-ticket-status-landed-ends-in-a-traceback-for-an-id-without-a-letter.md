# 208 — `append_task.py` glues a new row onto an unterminated `PROGRESS.csv` line, and `ticket_status.py landed NN` ends in a traceback for a ticket with no letter-led id

**Status:** queued
**Origin:** operator review (`bugs-to-review/test_append_task_missing_final_newline.py`, `test_ticket_status_landed_next_steps.py`), probed on `arena` @ `00355fd`
**Severity:** MEDIUM (40: the second ticket is handed out again, round after round — silent), LOW (41: a traceback after the status was already committed)
**File:** scripts/append_task.py
**Symbol:** main (+ scripts/ticket_status.py next_steps)
**Round:** 208
**Size:** S
**Also touches:** scripts/ticket_status.py, tests_bugfix/

## Why

Both scripts are the operator's and the coder's own loop: `append_task.py` records a ticket's outcome, `next_task.py` reads the record to hand out the next ticket, `ticket_status.py landed` flips a status and tells the operator what to do next. Each has one input shape it does not survive. Probed on `arena` @ `00355fd`.

## Bugs (each probed)

**40 — a row written after an unterminated last line is glued onto it** (`append_task.py`: `csv.DictWriter.writerow` on a file opened `"a"`).
`csv` writes the row where the file ends. A `PROGRESS.csv` that an editor, a hand or another tool left without its final newline therefore gets the new row on the same line. Probed:

```
$ printf 'ticket,outcome,commit,finding,note\n01-a.md,FIXED,abc,,' > P.csv
$ python3 scripts/append_task.py --progress P.csv --ticket 02-b.md --outcome FIXED --commit def
recorded #1: FIXED 02-b.md @ def -> P.csv
$ cat P.csv
ticket,outcome,commit,finding,note
01-a.md,FIXED,abc,,02-b.md,,FIXED,def,
$ python3 -c "import next_task as nt; print(nt.recorded('P.csv'))"
{'01-a.md': 'FIXED'}
```

The script says `recorded #1`, `next_task.py` reads one line as the *first* ticket's row only, and `02-b.md` is not recorded: it is handed out again, round after round. Fix: before writing, if the file is non-empty and its last byte is not `\n` (or `\r`), write the file's own line ending first (detect `\r\n` from the file; default `\n`); a terminated file gets no blank line; a new file still gets its header. Count the `recorded #N` by rows, not by lines.

**41 — `ticket_status.py landed NN` raises `AttributeError` for a ticket whose id does not start with a letter** (`ticket_status.py:126`: `re.match(r"[A-Za-z]+-?", titles.get(n, "")).group(0)`).
`title_id` falls back to the file name when a ticket has no `# heading`, and a heading may start with a number (`# 150 — …`, like 199–208 in this tree): either way `re.match` is `None` and `.group` raises — *after* the status line was flipped and committed. The operator gets a stack trace instead of the "push, and the tickets still to land" they ran the command for (probed through the script's own test on this tree: `AttributeError: 'NoneType' object has no attribute 'group'`). Fix: `m = re.match(...)`; `family = m.group(0) if m else ""`; a ticket without a family lists every queued/open ticket; a ticket with a family (`KC-9`) still lists its own epic first.

## Tests

One file per bug in `tests_bugfix/` (`test_<script>_<what>_208.py`), each failing on the old code:
40 — a row after an unterminated last line is its own row; the new row keeps the file's own line ending (`\r\n` file → `\r\n` row); a terminated file gets no blank line; a new file still gets its header and first row; `next_task.recorded` then sees both tickets;
41 — landing a ticket whose heading starts with a number, and one with no heading at all, prints what comes next (exit 0, the `git push` line present, the status already committed); a ticket with a family still lists its own epic first.

## Acceptance

```bash
python3 -m pytest tests_bugfix -k "208" -q
python3 -m pytest tests -k "append_task or ticket_status or next_task" -q
python3 -m pytest .smoke_tests/ -q
```
