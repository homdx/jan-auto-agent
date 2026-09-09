# V4 — Rows: `fails_open`, `risk`

**Severity:** HIGH  
**File:** `tools/auto/context_assembler.py`  
**Symbol:** `—`  
**Round:** 7 of 24  
**Size:** S  
**Source:** `docs/collect-epics/PLAN-v2.md` § V4  
**Depends on:** V1, V2  

---

**Priority:** High · **Size:** S · **Files:** `tools/auto/context_assembler.py`
**Depends on:** V1, V2 *(v1: A6 + A7, with the `guarded` row cut)*

**Do**

1. `fails_open` from `fail_open_registry` (117 sites over 64 modules): the count,
   then up to 4 `:line type` pairs, then the sentence that is the point of the
   row — `— these swallow exceptions on purpose, do not add error handling here`.
   A site *with* a rationale renders it; only 22 of 117 have one, so the row
   must read correctly without.
2. `risk` from `risk_index`, rendering the **components**, never the raw score
   (3138 for `coder.py` — unbounded and meaningless in a prompt):
   `loc 2507 · imported by 25 · unguarded accesses 45 · covered yes`.
   Suppress the row when nothing in it is notable.
3. **No `guarded` row.** Measured: 23 GUARDED locations across 469 modules, on
   15 modules. A row that fires on 3% of files and says "0" on the rest is
   budget spent on nothing. `guarded_accesses` keeps its one real consumer
   (`is_safe`, Stage 3).
4. `owns_config` is **not** a new row — `config_map.readers` encodes sharing,
   not ownership. Instead, today's `config_read` row gains a co-reader count:
   `[api] active (+10 other readers)`. That is the sibling-drift signal, in one
   parenthesis, with no new row and no new table.

**Acceptance**

- [ ] `coder.py` renders 7 fail-open sites and the warning sentence.
- [ ] A module with no fail-open sites renders no row (not "0").
- [ ] The `risk` row never prints the composite score.
- [ ] Both rows are `static` provenance; no LLM text reaches them.
- [ ] New: `tests/test_collect_pack_safety_rows.py`.

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
