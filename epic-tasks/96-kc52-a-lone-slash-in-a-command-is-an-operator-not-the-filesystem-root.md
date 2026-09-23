# KC-52 — a lone `/` in a command is an operator, not the filesystem root

**Status:** queued — found live 2026-09-23 in round 86 (run 4, base `27adb12`).
**Severity:** MEDIUM (a mechanical, gate-free reject of a harmless inline Python script)
**File:** `tools/contest/policy.py`
**Symbol:** `_command_paths`, `_pathlike`
**Round:** 96
**Size:** S
**Source:** round 86, `contest-out/86/sensenova-6-8-flash-lite-var2/decisions.jsonl`:
the agent edited its own test through `python3 - <<'PY' … PY`, and the script
held `tmp_path / "logs"`. The reject:

```
{"layer": "mechanical", "reply": "reject", "reason": "forbidden: / is at or under /"}
```

`_command_paths` splits the whole command, heredoc body included, into tokens.
The division operator `/` is a token, `_pathlike` accepts it because it starts
with `/`, it resolves to `/`, and `/` is the exact-only entry of
`HARD_DENYLIST`. The same happens for `a / b` in `python3 -c "…"`, `awk`,
`bc` and `expr`.

**Depends on:** KC-13 (the command scan, landed `3e4b41b`).
**Also touches:** `tests/test_contest_policy.py`

---

## What must change

`_command_paths` drops a token that is exactly `/` (after quote stripping). No
other token changes: `/etc`, `/tmp/x`, `//x` are still paths.

A command that really targets the root is still caught:
- `rm -rf /*` by `deny_commands` (`contest.ini`), unchanged;
- `ls /` becomes a no-path ask, answered `once` by layer 1. Listing the root
  is harmless, and every path *under* it (`/etc/passwd`) is still a path.
- `external_directory` patterns are not scanned by `_command_paths`: a `"/"`
  pattern there is unchanged.

## Acceptance

- [ ] `python3 - <<'PY'\nx = tmp_path / "logs"\nPY` → `("once", "mechanical")`, no gate call.
- [ ] `python3 -c "print(6 / 3)"` → `("once", "mechanical")`.
- [ ] `cat /etc/passwd` → unchanged (gate); `rm -rf /*` → unchanged (`deny_commands`).
- [ ] An `external_directory` event with `patterns: ["/"]` → unchanged (`forbidden`).
- [ ] Every existing policy test green; `tests` and `tests_bugfix` green; `CollectBridge._shrink` byte-identical.
