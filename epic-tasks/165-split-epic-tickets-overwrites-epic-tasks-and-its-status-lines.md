# 165 — `scripts/split_epic_tickets.py` overwrites `epic-tasks/` and every ticket's **Status:** line by default

**Status:** landed
**Origin:** `arena-bugs-sonet5` ticket 177
**Severity:** HIGH
**File:** scripts/split_epic_tickets.py
**Symbol:** main
**Round:** 165
**Size:** XS
**Also touches:** tests/test_split_epic_tickets_no_overwrite.py

## The bug

The script's default `--out` is `epic-tasks` — the folder where the round's **own** tickets live — and it writes every `NN-<id>-<slug>.md` and `INDEX.md` with `open(…, "w")` and no check. The regenerated ticket has no `**Status:**` line at all (the epics have none), and `INDEX.md` is rebuilt from scratch. Reproduced on a copy of this repo's `epic-tasks/`:

```
before: 01-l2-…md  "**Status:** landed — `8212df1`"      INDEX.md 183 lines (the whole 160-ticket history)
$ python3 scripts/split_epic_tickets.py --out <copy>
after : 01-l2-…md  no Status line                          INDEX.md 27 tickets
```

One run of the documented command (`python3 scripts/split_epic_tickets.py`) erases what landed, what is queued and every sha for the first 27 tickets and replaces the index of all 160 — the exact data `arena issue list`, `ticket_status.py` and `next_task.py` read. Recoverable from git only if the operator notices before committing.

## Fix

- Before writing anything, list which of the files this run would write already exist; if any do and `--force` is not given, print one refusal line (count, the first names, "Status lines would be lost; pick another --out or pass --force") and exit 1 with **nothing written** (all or nothing). `INDEX.md` alone is enough to refuse.
- `--force` keeps the old behaviour. `main(argv=None)` so a test can call it.

## Tests

`tests/test_split_epic_tickets_no_overwrite.py`: a fresh folder is written; a second run on a folder whose first ticket carries `**Status:** landed` and whose index was edited exits 1 and leaves both byte for byte; a lone `INDEX.md` refuses and writes no ticket; `--force` overwrites.

## Acceptance

```bash
python3 -m pytest tests/test_split_epic_tickets_no_overwrite.py -q
python3 scripts/sync_test_tiers.py --check
```
