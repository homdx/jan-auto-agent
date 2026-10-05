# 183 — `TicketStore.create` lets two racing creates of one id both succeed

**Status:** landed
**Origin:** `arena-bugs-sonet5` ticket 180 — late commit 2b78ee5; a race only two concurrent creators of one id can hit, the pipeline creates tickets one at a time
**Severity:** MEDIUM
**File:** tools/auto/ticket_store.py
**Symbol:** TicketStore.create
**Round:** 183
**Size:** S
**Also touches:** tools/auto/utils.py, tests/test_ticket_store_create_race.py

## The bug

`create` checked `path.exists()` and then wrote with `atomic_write_text` (temp file + `os.replace`). `os.replace` overwrites, so two creates of the same id that both pass the check both "succeed" and the second ticket silently replaces the first — `TicketAlreadyExists` (the documented guard) never fires, and the first caller believes its ticket is stored.

Reproduced with a barrier between the check and the write: both threads return normally, `get()` shows the later body.

## Fix

`atomic_write_text(..., exclusive=True)` hard-links the finished temp file into place (`os.link` raises `FileExistsError` if the target exists), so exactly one writer wins; `create` maps `FileExistsError` to `TicketAlreadyExists`. The temp file is removed on the loser's path; filesystems without hard links fall back to the old replace.

## Tests

`tests/test_ticket_store_create_race.py`: racing creates yield one `ok` and one `exists` (the body is the winner's); no stray temp file is left behind.

## Acceptance

```bash
python3 -m pytest tests/test_ticket_store_create_race.py -q -n 0
python3 scripts/sync_test_tiers.py --check
```
