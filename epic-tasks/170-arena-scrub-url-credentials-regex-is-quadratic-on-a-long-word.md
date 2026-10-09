# 170 — `output.scrub` stalls for seconds on a long word: the URL-credentials pattern is O(n²)

**Status:** landed
**Origin:** `arena-bugs-opus5` ticket 166
**Severity:** MEDIUM
**File:** tools/arena/output.py
**Symbol:** _URL_USERINFO, scrub
**Round:** 170
**Size:** S
**Also touches:** tests_bugfix/test_arena_scrub_169_170.py

Found while re-reviewing the first fix for 169.

## The bug

`_URL_USERINFO = (?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*://)[^/@\s]+@` can start at **every** letter of a word. On a word with no `://`, each start scans to the word's end and fails, so a word of n characters costs n²/2 steps. Measured: `scrub("x" * 100000)` 13 s, `scrub("a-" * 20000)` 1 s.

`scrub` runs on every line arena prints — `refuse` of a git/kilo stderr, `_run_child`'s run line, `emit` over every cell, `models._hide` of a provider's error body. A provider answering with a long base64/minified blob, or a long `kilo` stderr line, freezes the command for seconds to minutes.

The first fix for 169 had the same shape (`(?:[A-Za-z0-9]+[_\-.])*` before the secret word): 25 s on `"a-" * 20000`. It was replaced before the push of this ticket.

## Fix

- `_URL_USERINFO` gets the look-behind `(?<![A-Za-z0-9+.\-])`: a scheme starts only where a scheme-character run starts, so each run is scanned once.
- 169's pattern is one flat class `[A-Za-z0-9_.\-]+=` behind the same kind of look-behind; whether the name ends in a secret word is decided in a callback (`_kv_sub`), not by the regex.

## Tests

1. `scrub` of `"x" * 100000`, `"a-" * 50000`, `"a." * 50000` and `"k=v " * 30000` each under 2 s (was 13 s / 1 s+).
2. `https://u:p@h/x`, `git+ssh://me:pw@host/r`, `(http://a:b@c)` are still masked.
