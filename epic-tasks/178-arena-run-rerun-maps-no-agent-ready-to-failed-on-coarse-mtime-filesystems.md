# 178 — `arena run rerun` reports a plain failure instead of "no agent ready" when the runner rewrites `state.json` in the same mtime tick

**Status:** landed
**Origin:** `arena-bugs-opus5` ticket 163 — not hit on ext4/xfs/btrfs (ns mtimes); coarse-mtime filesystems only
**Severity:** LOW
**File:** tools/arena/rounds.py
**Symbol:** run_rerun, _run_child, _map_exit
**Round:** 178
**Size:** S
**Also touches:** tests_bugfix/test_arena_rounds_robustness_177_178.py

## The bug

`_map_exit` turns runner exit 2 into `EXIT_NO_READY` (4) only when `state.json`'s mtime is `>= started`. `run start` passes `started = time.time() - 1` — "one second of slack: some filesystems keep mtimes in whole seconds". `run rerun` instead passes `math.nextafter(wrote, math.inf)`, i.e. strictly later than its own write. On a filesystem with whole-second (or 2 s FAT / coarse) mtimes, a runner that rewrites `state.json` within the same tick gets the same mtime as arena's write, `mtime >= started` is False, and the exit is mapped to `EXIT_FAILED` (1).

Reproduced:

```
mtime = w (whole second); _map_exit(2, state, nextafter(w, inf)) -> 1   # expected 4
```

A script driving `arena run rerun` then retries/alerts as a crash when the round actually ran and simply had nobody ready.

## Fix

Detect the runner's rewrite by content, not by mtime alone: remember the bytes (or a hash / the `(st_mtime_ns, st_size, st_ino)` tuple) arena wrote, and treat any difference after the child exits as "ran". Keep the mtime check for `run start`.

## Tests

1. Stub child exits 2 after rewriting `state.json` with a different body, mtime forced equal to arena's write → exit 4.
2. Stub child exits 2 without touching the file → exit 1.

## Review note

On Linux ext4/xfs/btrfs mtimes have nanosecond precision, so this happens only on coarse-mtime filesystems (FAT/exFAT, some network mounts). The authors already plan for those (`run start`'s one-second slack), which is why `rerun` diverging from it is still a bug and not a theoretical one.
