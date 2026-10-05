# 185 — arena: a CRLF ticket read from git loses its carriage returns

**Status:** landed
**Origin:** `arena-bugs-opus5` ticket 183 — a refinement of 174 (its surrogateescape text mode still applied universal newlines)
**Severity:** LOW
**File:** tools/arena/gitref.py
**Symbol:** git
**Round:** 185
**Size:** XS
**Also touches:** tests_bugfix/test_arena_crlf_ticket_blob_185.py

## Bug
`gitref.git` ran git in text mode (`encoding=…`). Text mode applies universal
newlines to stdout, so `git show branch:epic-tasks/NN-x.md` of a ticket saved
with CRLF (or a lone CR) came back with `\n` only. Bug 174 promised "a blob
read here and fed back is the same blob" — not true for these files:

- `build_round_ref`: `hash-object` of the read text ≠ the blob on the tip, so a
  ticket that **is** on the integration tip was committed again on
  `arena-round/NN` as a "ticket for the round" commit that only rewrote the
  line endings;
- a later `arena run start NN` compared the same wrong text and could report
  "holds another ticket text" depending on which source the ticket came from.

## Fix
Run git in bytes mode; decode stdout/stderr and encode stdin with UTF-8 +
`surrogateescape` by hand. Every byte, `\r` included, round-trips.

## Tests
`tests_bugfix/test_arena_crlf_ticket_blob_185.py` — all three fail on the old
code (`\r` lost, extra commit, hash mismatch).

**Impact here:** none today (every ticket on this disk is LF); it bites a ticket saved with CRLF by an editor on another machine, which `arena run start` would commit again on `arena-round/NN` with LF endings.
