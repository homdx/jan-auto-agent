# KC-53 — the round prompt names the scratch paths that need no reviewer

**Status:** queued — found live 2026-09-23 in round 86 (run 4, base `27adb12`).
**Severity:** MEDIUM (agents invent their own `/tmp/…` folders, every command there goes to the gate, and a failing gate refuses them)
**File:** `tools/contest/runner.py`
**Symbol:** `_PROMPT`, `round_prompt`, `run_agent` (its two `round_prompt` calls)
**Round:** 97
**Size:** S
**Source:** round 86 run 4: `hy3` wrote its test fixtures to `/tmp/scratch_suite`, outside `tmp_roots` (`/tmp/kilo/*`, `/tmp/contest/*`), and 6 of its commands came back `gate-failed` / `empty reply`. The prompt says "copy `agents_128k.ini` to a scratch path" and "any command that reaches outside your worktree is decided by a reviewer", but never says which scratch paths are free. Commands in `decisions.jsonl` across `contest-out/` name `/tmp/scratch_suite` 18 times, `/tmp/debug_argv.py` 8, `/tmp/test_wt` 5, `/tmp/fl_logs` 5, `/tmp/cli_fixed.py` 5 and more. None of them is under `tmp_roots`.
**Depends on:** KC-6 (`round_prompt`), KC-2 (`tmp_roots`).
**Also touches:** `tests/test_contest_runner.py`

---

## What must change

1. `round_prompt(..., tmp_roots=())`. When *tmp_roots* is non-empty, the
   prompt gains one paragraph before the reviewer sentence:

   ```
   Scratch space outside your worktree that needs no reviewer: /tmp/kilo/*, /tmp/contest/*.
   Put your own scratch files under /tmp/contest/<name>/ — anywhere else in /tmp goes to the reviewer.
   ```

   `<name>` is the agent's name. The folder is suggested only when a root is
   literally `/tmp/contest/*`; otherwise only the list is printed.
2. `run_agent` passes `tuple(config.tmp_roots)` in both calls.
3. With no *tmp_roots*, the text is byte-identical to today's.

## Acceptance

- [ ] `round_prompt("zeta-9", …, tmp_roots=("/tmp/kilo/*", "/tmp/contest/*"))` contains both globs and `/tmp/contest/zeta-9/`.
- [ ] `round_prompt(…)` without *tmp_roots* is unchanged (the existing `test_round_prompt…` pass untouched).
- [ ] The tests that compare the first prompt to `round_prompt(...)` (`test_a_fresh_round_reads_no_tree_and_its_prompt_is_exactly_round_prompt` and the three resume tests) pass `tmp_roots=cfg.tmp_roots` in the expectation.
- [ ] `tests` and `tests_bugfix` green; `CollectBridge._shrink` byte-identical.
