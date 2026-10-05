# 167 — `arena model use` / `drop` mangle a multi-line `models =` value

**Status:** landed
**Origin:** `arena-bugs-opus5` ticket 168
**Severity:** MEDIUM
**File:** tools/arena/models.py
**Symbol:** _with_key
**Round:** 167
**Size:** S
**Also touches:** tests_bugfix/test_arena_ini_continuation_167.py

## Problem
`_with_key` edits `contest.local.ini` as text. To replace a key it removes the
key line and the indented lines under it, but it stopped at the first blank or
comment line. configparser (`empty_lines_in_values=True`, comment lines skipped)
keeps reading indented lines after either, so they are still part of the value.

## Reproduction
```ini
[arena.profile.p]
models = a,
  b,

  c
```
- `model use` (set `z`): the file becomes `models = z` + blank + `  c`, which
  configparser reads as `"z\n\nc"`. The stale model `c` stays in the profile.
- `model drop` that removes the key (`None`): the orphan `  c` line remains with
  no key above it, so it is a `ParsingError`. The whole `contest.local.ini` is
  now unreadable, so every contest/arena verb that reads it fails.
- The same happens with an indented `# comment` line inside the value.

## Fix
Scan to the section end. Skip blank and comment lines, extend the removal over
each indented line, and stop at the first unindented line. Blank lines and
comments after the value are kept.

## Tests
`tests_bugfix/test_arena_ini_continuation_167.py`: blank line inside the value,
key removal leaves a parseable ini, comment inside the value, trailing lines kept.
