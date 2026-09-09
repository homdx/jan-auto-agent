# V2 — The block becomes an ordered row list

**Severity:** HIGH  
**File:** `tools/auto/context_assembler.py`  
**Symbol:** `—`  
**Round:** 5 of 24  
**Size:** M  
**Source:** `docs/collect-epics/PLAN-v2.md` § V2  
**Depends on:** nothing  

---

**Priority:** High · **Size:** M · **Files:** `tools/auto/context_assembler.py`
**Depends on:** nothing *(v1: A3 + A4, without the new module)*

Behaviour-preserving. No new row types in this ticket, so a regression here is
unambiguous.

**Do**

1. Introduce `_PACK_ROWS` as in §3, populated with today's three renderers
   (`contract`, `config_read`, `public_symbols`) moved into `_row_*` functions
   verbatim.
2. `_COLLECT_HEADER` and the `module:` / `parse_error:` lines are unchanged and
   stay outside the loop.
3. `public_symbols` moves to last. Its silent `[:20]` becomes budget-driven with
   an announced remainder — `… (+12 more, cut for budget)` — matching the
   honesty `_format_module_block` already has.
4. Deduplicate identical rendered rows (24 duplicate `config_read` lines exist).
5. `budget=None` renders everything, so every existing caller and test keeps
   working before V6 wires the budget in.
6. The "header + bare module line only → return `''`" early-out is preserved.

**Acceptance**

- [ ] For every module whose old block was within budget, the new block carries
      the same facts (order may differ; content may not).
- [ ] The 28 modules with >20 symbols no longer drop 296 symbols silently.
- [ ] Duplicate `config_read` lines gone.
- [ ] `tests/test_collect_context_block*.py` updated, not deleted; the
      assertions that pinned content still hold.
- [ ] New: `tests_bugfix/test_collect_block_symbol_cap_announced.py`.

---

## Ground rules — these are the scoring criteria

1. **Verify before implementing.** Grep the live source for what this ticket
   claims. It was written against `competition` at `68b78a0`; if the code has
   moved, the ticket is the stale one — say so and adapt rather than forcing it.
2. **`CollectBridge._shrink` is not to be modified, reordered, or replaced.**
   New work runs *before* it, never instead of it. A diff that touches a line
   inside `_shrink` fails this round regardless of what else it does.
3. **One local commit for this ticket alone.** Never `git push`. No
   opportunistic refactors bundled in.
4. **Ships a test.** New behaviour → `tests/`; a fix to something that used to
   be wrong → `tests_bugfix/`. If you add a `tests/test_*.py`, run
   `python3 scripts/sync_test_tiers.py` so the `.smoke_tests/` mirror exists
   (a pre-commit hook enforces it).
5. **Run the suite as four separate invocations** — combining the roots in one
   `pytest` call produces ~362 false errors from a conftest collision:
   ```bash
   for d in tests tests_bugfix .smoke_tests .regression_tests; do python3 -m pytest "$d" -q --timeout=180; done
   ```
   `python` is not on PATH — use `python3`.
6. **Fail open.** A broken artifact, a malformed config key or an absent model
   degrades to "no collect data" and never raises into a run. Everything added
   here inherits that.
7. **Never widen provenance.** Static facts and LLM prose stay separately
   labelled. `is_safe()` keeps reading only `guarded_accesses` /
   `FAIL_OPEN_REGISTRY` / `CONTRACTS`.
8. **Never run anything against a live provider config.** Copy
   `agents_128k.ini` to a scratch path and point `base_url` at a stub before any
   measurement run.
