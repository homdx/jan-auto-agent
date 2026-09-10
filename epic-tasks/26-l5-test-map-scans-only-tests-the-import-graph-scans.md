# L5 — `test_map` scans only `tests/`; the import graph scans four roots

**Severity:** HIGH  
**File:** `tools/collect/test_map.py`  
**Symbol:** `—`  
**Round:** 26 of 27  
**Size:** S  
**Source:** `docs/collect-epics/EPIC-L-live-findings.md` § L5  
**Depends on:** V3 (the rows that show it).  

---

**Priority:** High · **Size:** S · **Files:** `tools/collect/test_map.py`
(possibly the scanner's root list). **Depends on:** V3 (the rows that show it).

### The observation

Every key in the artifact's `test_map` values starts with `tests/`. The graph's
`imported_by` sees `tests/`, `tests_bugfix/`, `.smoke_tests/`,
`.regression_tests/`. The V3 pack for `main.py` therefore says, three lines
apart:

```
callers: entry point — nothing in shipped code imports this; 13 test files do
tests:   5 files: tests/test_auto_g10.py, …, +2
```

Both numbers are true and the block still contradicts itself: 8 of the 13 are
in `tests_bugfix/` and `test_map` never looked there. This is the same
classification V1 shipped in `loader._TEST_PATH_PREFIXES`; `test_map` predates
it and uses its own.

### Do

1. `build_test_map` classifies test modules with the **same** rule V1's loader
   uses (`_TEST_PATH_PREFIXES` + `conftest.py`) — import it or move it to one
   shared place; do not keep two lists.
2. `.smoke_tests/` and `.regression_tests/` mirrors are symlinks into `tests/`;
   a symlinked test counts **once**, under its real path, so the count cannot
   double.
3. `zero_coverage` / `thin_coverage` are recomputed on the wider set; they will
   shrink. Record old and new counts in the commit message.

### Acceptance

- [ ] Fixture with `tests/test_a.py` and `tests_bugfix/test_b.py` both
      importing `m.py`: `test_map["m.py"]` has both.
- [ ] A symlinked mirror of `tests/test_a.py` does not add a second entry.
- [ ] Rebuilt against this repo: the `main.py` pack's `callers` test count and
      its `tests` row count are the same number.
- [ ] M1 re-run; `zero_coverage` before/after in the commit message.
- [ ] New: `tests/test_collect_test_map_all_roots.py`.

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
