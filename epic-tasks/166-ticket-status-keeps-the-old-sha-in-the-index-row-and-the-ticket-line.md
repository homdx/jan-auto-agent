# 166 — `scripts/ticket_status.py` keeps the previous sha in the INDEX row (and in the ticket line when reopening)

**Status:** landed
**Origin:** `arena-bugs-sonet5` ticket 178 — seen by sonet5 while fixing 177
**Severity:** MEDIUM
**File:** scripts/ticket_status.py
**Symbol:** main, INDEX_ROW
**Round:** 166
**Size:** XS
**Also touches:** tests/test_ticket_status_index_cell.py

## The bug

`INDEX_ROW` captured only the first word (`\S+`) of the row's status cell, so a flip rewrote that word and left the rest of the cell:

```
| 48 | `KC-9` | landed `abc1234` |   --open-->   | 48 | `KC-9` | open `abc1234` |
| 48 | `KC-9` | landed `abc1234` |   landed --sha def5678   -->   landed `abc1234`   (the new sha never reaches the row)
| 12 | `V8`   | **landed** (`da1e9b3`) |  --queued-->  queued (`da1e9b3`)   (`**landed**` is one \S+ token, the parenthesised sha stays)
```

So the index shows the wrong winner for a re-landed ticket, and an "open" ticket that claims a sha. The ticket's own line had the same fault when a landed ticket was reopened without `--note` (`open `abc1234` — won`).

## Fix

The whole status cell is rewritten (`landed `SHA`` for landed, the bare word otherwise); a landed ticket sent back to open/queued with no `--note` drops its old trailing text.

## Tests

`tests/test_ticket_status_index_cell.py`: reopen drops the sha from row and ticket; re-landing replaces the sha; bold/dated cells are replaced whole.

## Acceptance

```bash
python3 -m pytest tests/test_ticket_status_index_cell.py -q
python3 scripts/sync_test_tiers.py --check
```
