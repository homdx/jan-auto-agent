# 176 — `arena issue list` does not read `151, 152: …` as naming tickets 151 and 152

**Status:** landed
**Origin:** `arena-bugs-sonet5` ticket 169 — the grouped commits of this very change name several tickets (`NN, MM: …`)
**Severity:** LOW
**File:** tools/arena/tickets.py
**Symbol:** _SUBJECT_RE, _commit_numbers, scan (the `status landed but no NN: commit` flag)
**Round:** 176
**Size:** XS
**Also touches:** tests/test_arena_issue_list.py

## The bug

`_SUBJECT_RE = ^0*(\d+)(?::|\s)` reads one number at the start of a subject. A subject that names **several** tickets, which this branch's own history has (`3416cbe 151, 152: tickets for the event-tap reconnect and .kilo guards …`), matches nothing: `151,` is neither `NN:` nor `NN␣`.

```
>>> tickets._SUBJECT_RE.match("151, 152: tickets for the tap reconnect")
None
```

Ticket 152 therefore shows `status landed but no 152: commit on <branch>` until a later commit that starts `152:` exists, and a `NN:` commit that only appears in a list never clears the `commit NN: on <branch> but status …` check either.

## Review (second pass) — what was dropped

The first draft of this ticket also called `3 flaky tests fixed` → ticket 3 a bug. That is **by design**: ticket 144 (the AR-6 spec) says a subject "starts with `NN:` (or `NN ` / `NN —`)", and the round's commit convention is `NN: …`. It is a known trade-off of the spec, not a defect, and is left alone here.

## Fix

- Accept the list form `NN, MM[, …]:` (numbers separated by `,` and optional spaces, ending in `:`); `_commit_numbers` returns every number of it.
- Everything the spec already accepts (`NN:`, `NN —`, `NN␣`, zero-padded) is unchanged.

## Tests

1. `151, 152: x` → `{151, 152}`; `144: x`, `144 — x`, `0144: x` → `{144}`.
2. A repo whose only subject for 152 is `151, 152: …` gives a `landed` ticket 152 no `no 152: commit` flag.

## Acceptance

```bash
python3 -m pytest tests/test_arena_issue_list.py -n 4 -q
```
