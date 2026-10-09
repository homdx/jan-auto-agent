# 155 — the `.kilo` ignore is trusted from a per-process cache: once the file is gone, the next push hides nothing

**Status:** landed
**Severity:** MEDIUM
**File:** tools/contest/backend.py
**Symbol:** _exclude_kilo_dir, _drop_kilo_ignore, _KILO_EXCLUDED
**Round:** 155
**Size:** XS
**Also touches:** tests/test_contest_kilo_tap_reconnect.py

**Depends on:** 151, 154.

Found while judging round 151 — first by reading the drop, then by the bench's B11 and the cross-tests, which showed that clearing the cache in the drop was not enough.

## The bug

`_exclude_kilo_dir` remembered a directory in `_KILO_EXCLUDED` once its `.kilo/.gitignore` was in place and never looked again. The file can disappear behind that cache in the same process:

- `drop_stale_kilo_file` → `_drop_kilo_ignore` deletes it (and an empty `.kilo/`) at the end of a run, and a relay (`--legs`, KC-43) runs its next leg in the same process and the same worktrees;
- the agent's own `git clean -fdx` or `rm -rf .kilo` between two pushes.

The next push then skips the ignore, the `PATCH /config` creates `.kilo/kilo.jsonc`, Kilo's reload writes its own `.kilo/.gitignore` without the project names (ticket 154's fact), and the window is visible to the agent's `git add -A`.

## Fix

No cache: `_exclude_kilo_dir` checks the file at every push (one `git rev-parse` and one read, once per remembered size). `_KILO_EXCLUDED` is gone, and with it the test fixture that cleared it.

## Tests

1. Push, drop (file and `.kilo/` gone), second push in the same process: `kilo.jsonc` is ignored, `git status` clean.
2. Push, `rm -rf .kilo` with no drop, second push: the same. Fails with a cache that only the drop clears.
3. Bench 151 B11 (two backends, `.kilo/` removed between the pushes).

## Acceptance

```bash
python3 -m pytest tests/test_contest_kilo_tap_reconnect.py -q
```
