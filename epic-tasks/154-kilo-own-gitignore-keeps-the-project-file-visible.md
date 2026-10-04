# 154 — Kilo's own `.kilo/.gitignore` leaves the pushed `kilo.jsonc` in plain view

**Status:** landed
**Severity:** HIGH
**File:** tools/contest/backend.py
**Symbol:** _exclude_kilo_dir
**Round:** 154
**Size:** S
**Also touches:** tests/test_contest_kilo_tap_reconnect.py

**Depends on:** 151.

Found while judging round 151 (the winner's guard, 808cdf8); the fake server never writes the file, so no offline test saw it.

## The bug

151 keeps the project file out of git with a `.kilo/.gitignore` in the workspace (`kilo.jsonc`, `kilo.json`, `.gitignore`). A `.kilo/.gitignore` that is already there with other content is taken for the agent's and only warned about. Live, Kilo 7.6.2 writes its own one — `node_modules`, `package.json`, `package-lock.json`, `pnpm-lock.yaml`, `bun.lock`, `yarn.lock`, `.gitignore`, `agent-manager.json`, no trailing newline — whenever an instance opens a workspace that has a `.kilo/` and no `.gitignore` (create-if-missing, never rewritten). So a workspace where the agent made `.kilo/rules/`, or any `.kilo/` Kilo saw first, gets the warning and no ignore: the next `PATCH /config` writes `.kilo/kilo.jsonc` untracked and visible, and the agent's `git add -A` puts the window in its diff — after which the file is tracked and every later push is refused.

## Fix

A `.kilo/.gitignore` that ignores itself (has a `.gitignore` line) and is not tracked (`git ls-files` answers, empty) can never reach the agent's diff, whoever wrote it: the missing project names are appended to it, Kilo's lines kept. A tracked one, one that does not hide itself, or a `git` that does not answer is still the agent's and still only warned about.

## Tests

1. Kilo's exact file under `.kilo/` next to an agent rule: after the guard, Kilo's lines are first and unchanged, `kilo.jsonc` is ignored, the rule is not, `git status` shows only the rule. Fails on 808cdf8.
2. A tracked, self-listing `.kilo/.gitignore` is byte-identical afterwards and a warning is logged.

## Acceptance

```bash
python3 -m pytest tests/test_contest_kilo_tap_reconnect.py -q
```

```bash
python3 -m pytest tests -n 8 -q
```
