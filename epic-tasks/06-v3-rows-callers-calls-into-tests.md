# V3 — Rows: `callers`, `calls_into`, `tests`

**Severity:** HIGH  
**File:** `tools/auto/context_assembler.py`  
**Symbol:** `—`  
**Round:** 6 of 24  
**Size:** S  
**Source:** `docs/collect-epics/PLAN-v2.md` § V3  
**Depends on:** V1, V2  

---

**Priority:** High · **Size:** S · **Files:** `tools/auto/context_assembler.py`
**Depends on:** V1, V2 *(v1: A5)*

The first rows that carry new information.

**Do**

1. `callers` renders the **count first**, then up to 5 names:
   `25 modules import this (7 non-test): controller.py, inner_loop.py, +5`.
   The count is the fact; the names are the courtesy.
2. `calls_into` — up to 5 first-party imports.
3. `tests` — from `test_map` (301 entries, 78 modules covered), up to 3 names
   plus a count. When the module is in `zero_coverage` (223 of 469), render
   `no test covers this file` instead. **The absence is the more useful fact**
   and costs nothing.
4. An entry-point module with no callers renders
   `callers (entry point — nothing imports this)`, not an empty row.
5. Paths always relative.

**Acceptance**

- [ ] `tools/auto/coder.py` names its 7 non-test importers and says 25.
- [ ] A `zero_coverage` module renders the no-tests form.
- [ ] `main.py` renders the entry-point form.
- [ ] A module absent from the model still yields `""` overall.
- [ ] New: `tests/test_collect_pack_neighbourhood_rows.py`.

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
