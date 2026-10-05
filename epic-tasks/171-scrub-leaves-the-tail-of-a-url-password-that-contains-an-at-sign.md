# 171 — `output.scrub` leaves part of a URL password that contains `@`

**Status:** landed
**Origin:** `arena-bugs-sonet5` ticket 170
**Severity:** LOW
**File:** tools/arena/output.py
**Symbol:** _URL_USERINFO, scrub
**Round:** 171
**Size:** XS
**Also touches:** tests/test_arena_cli.py

## The bug

`_URL_USERINFO = (scheme://)[^/@\s]+@` stops at the **first** `@`. A userinfo whose password holds a literal `@` (written unencoded in a `base_url`, which providers' dashboards and `.env` files commonly produce) keeps everything after it:

```
>>> output.scrub("postgres://user:p@ss@host/db")
'postgres://***@ss@host/db'
```

`ss` — the tail of the password — is printed by every path that goes through `output.scrub` (`run start`'s echo of the run line, `run view`, `issue` output, `model` commands' base URLs). The module's own contract is "a `base_url` carrying `user:pass@` is never printed".

## Fix

- Match the userinfo up to the **last** `@` before the first `/`, `?`, `#` or whitespace: `(?P<scheme>…://)[^/?#\s]*@`. A host-only URL has no `@` and is untouched; `https://x.com/a@b` (the `@` after the path) is untouched because the class stops at the first `/`.

## Tests

1. `postgres://user:p@ss@host/db` → `postgres://***@host/db`; `https://u:p%40ss@h/x` → `https://***@h/x`.
2. `https://x.com/a@b` and `mailto:a@b.c` unchanged.
3. The existing `api_key=` / `token=` cases unchanged.

## Acceptance

```bash
python3 -m pytest tests/test_arena_cli.py -n 4 -q
```

**Landed:** merged with 170's look-behind: the userinfo class is `[^/?#\s]+` behind `(?<![A-Za-z0-9+.\-])`, still linear on a long word and now up to the last `@` of the userinfo.
