# 177 — `arena run list` crashes with a traceback on an unpadded round folder (`7`, `8.1`)

**Status:** landed
**Origin:** `arena-bugs-opus5` ticket 162
**Severity:** LOW
**File:** tools/arena/rounds.py
**Symbol:** list_rows, round_folder
**Round:** 177
**Size:** S
**Also touches:** tests_bugfix/test_arena_rounds_robustness_177_178.py

## The bug

`list_rows` groups every folder matching `_ROUND_DIR` (`^(\d+)(?:\.(\d+))?$`) by `int(group(1))`, so `7`, `07` and `8.1` are all accepted. It then asks `round_folder(repo, config, nn)`, which always builds the **zero-padded** `<out_dir>/07` (`contest_cli._round_out_dir`) and looks for legs named `07.K`. For an unpadded folder that path does not exist, `state.stat()` fails and the fallback `last.stat()` raises `FileNotFoundError` out of `run list` — the module docstring promises "a `?` row, never a traceback".

Reproduced offline:

```
contest-out/7/      -> CRASH FileNotFoundError: .../contest-out/07
contest-out/8.1/    -> CRASH FileNotFoundError: .../contest-out/08
```

Unpadded folders are real: `tickets._round_entries` documents "zero-padded or not, both are read as integers", an older round or a manual `--out contest-out/7` produces one, and one such folder makes the whole listing unusable.

## Fix

- In `list_rows`, use the folders it actually found (`folders`) to pick the last one (highest leg, else the base), not `round_folder`'s re-derived path; or skip/`?`-row a group whose derived folder is missing.
- Guard `last.stat()` with `OSError` → `?` row, mtime 0.

## Tests

1. `contest-out/7/` alone → one row `RUN 7, STATE ?`, exit 0.
2. `contest-out/8.1/state.json` alone → one row for 8 with `LEGS 1/1`.
3. Padded folders keep today's rows.

## Review note

Reachability re-checked: arena itself never creates an unpadded folder (`--out` is arena-owned, `_round_out_dir` pads), so this needs a folder from an older round or a direct `python3 -m tools.contest run --out contest-out/7`. Still a traceback where the module promises a `?` row; severity lowered to LOW.
