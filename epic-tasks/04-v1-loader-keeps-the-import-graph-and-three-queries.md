# V1 — Loader keeps the import graph, and three queries on top

**Severity:** HIGH  
**File:** `tools/collect/loader.py`  
**Symbol:** `—`  
**Round:** 4 of 27  
**Size:** S  
**Source:** `docs/collect-epics/PLAN-v2.md` § V1  

---

**Priority:** High · **Size:** S · **Files:** `tools/collect/loader.py`
*(v1: A1 + A2, minus `sibling_gaps`)*

`_load_from_dir` reads 9 of 13 artifact keys. `import_edges` (469),
`imported_by` (469) and `entry_points` (382) are written and thrown away on
read. `sibling_gaps` is **not** recovered — it has 0 entries.

**Do**

1. Add `import_edges`, `imported_by`, `entry_points` to `CollectModel`,
   populated inside the existing guarded `try:` so a malformed shape still
   degrades to `_absent()`.
2. Defaults are empty containers, never `None`.
3. Three read-only queries, each returning empty on an absent model:
   ```python
   def callers_of(self, path, *, exclude_tests=True, limit=0) -> List[str]
   def calls_into(self, path, *, limit=0) -> List[str]      # first-party only
   def risk_for(self, path) -> Optional[dict]
   ```
   `exclude_tests=True` is the default and is not decoration: 8 of the first 8
   importers of `coder.py` are test files, and they are already the `tests` row.
   Order by the caller's own `risk_index` blast radius, then path, so a
   truncated list keeps the callers that matter and two `load()` calls agree
   (COLLECT-3 determinism).

**Acceptance**

- [ ] `callers_of("tools/auto/coder.py")` returns only non-test modules, stable
      across two loads; with `exclude_tests=False`, every importer the artifact
      records. Against the artifact on disk that is **2 and 25** — re-measure
      rather than trusting the figure: an earlier draft of this plan said 7
      non-test, which was measured on a different tree state and does not
      reproduce.
- [ ] An artifact missing these keys loads with empty containers — not an
      absent model. Both schema directions.
- [ ] Absent model → `[]` / `None`, no exception.
- [ ] Existing `tests/test_collect_loader*.py` pass unchanged.
- [ ] New: `tests/test_collect_loader_graph_tables.py`.

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
