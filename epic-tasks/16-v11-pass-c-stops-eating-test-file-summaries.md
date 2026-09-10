# V11 — Pass C stops eating test-file summaries

**Severity:** HIGH  
**File:** `tools/collect/verifier.py`  
**Symbol:** `—`  
**Round:** 16 of 27  
**Size:** M  
**Source:** `docs/collect-epics/PLAN-v2.md` § V11  
**Depends on:** nothing  

---

**Priority:** High · **Size:** M · **Files:** `tools/collect/verifier.py`
**Depends on:** nothing *(v1: B7)*

Pass C drops a claim whose cited symbol belongs to another module. For a test
file that rule is inverted from reality: a test's entire purpose is to name
symbols from the module under test.

```
claims extracted             2238
dropped                      1368  (61%)
modules left with no purpose  228
  of which test files         219   ← this ticket
  of which source files         9
```

This is also what makes V5 worth its budget: every recovered purpose is a
neighbour row that can render.

**Do**

1. In `verify_repo`, when the module under verification is a test file
   (`tests/`, `tests_bugfix/`, `.smoke_tests/`, `.regression_tests/`, or
   `test_*.py` / `*_test.py` anywhere), extend the citable-symbol set from
   "symbols defined here" to "symbols defined here **plus every module this test
   imports**".
2. `import_edges` already knows what a test imports — no new analysis.
3. A test citing a symbol from a module it does **not** import is still a
   hallucination and still drops.
4. Make it a parameter of the verification call, not a filename check buried in
   the claim extractor, so the rule is visible and testable.

**Acceptance**

- [ ] A full rebuild leaves ≤20 modules with an empty purpose (from 228); record
      the exact number.
- [ ] A test citing an unimported symbol still drops.
- [ ] Non-test rules unchanged — the 9 source-file drops stay dropped.
- [ ] New: `tests_bugfix/test_collect_verifier_test_file_citations.py`.

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
