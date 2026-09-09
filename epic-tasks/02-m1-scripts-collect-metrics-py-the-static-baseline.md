# M1 — `scripts/collect_metrics.py` — the static baseline

**Severity:** CRITICAL  
**File:** `scripts/collect_metrics.py`  
**Symbol:** `—`  
**Round:** 2 of 24  
**Size:** M  
**Source:** `docs/collect-epics/EPIC-M-metrics.md` § M1  
**Depends on:** nothing. **Must land before any other ticket in any epic.**  

---

**Priority:** Highest · **Size:** M · **Files:** `scripts/collect_metrics.py` (new)
**Depends on:** nothing. **Must land before any other ticket in any epic.**

A read-only script over one `.collect/` directory. No repo scan, no LLM, no
network. Two outputs: a human table on stdout, and `--json` for diffing.

```bash
python3 scripts/collect_metrics.py --collect-dir ../jan-to-fix-pull-v2/.collect \
    --json docs/collect-epics/baseline.json
```

### Metric set — Tier 0 and Tier 1, static

```
artifact
  modules                          469
  bytes                            2 284 645
  public_symbols                   4030      (unique qualnames 4030)
  signatures still elided "name(…)"  4030 / 4030      → EPIC C4
  methods indexed                     0               (top-level scan only)

tables and their reach
  import_edges / imported_by       469 / 469     dropped by loader today
  entry_points                     382           dropped by loader today
  sibling_gaps                     0             ← empty; do not build for it
  test_map entries                 301           modules covered  78 / 469
  zero_coverage                    223           thin_coverage    10
  risk_index                       469
  config_map                       186
  contracts                        4
  fail_open_registry               117           with a rationale  22
  guarded_accesses records         2814          distinct locations 2646
      all-GUARDED locations        23            mixed 3   all-UNGUARDED 2620
      modules with >=1 GUARDED     15 / 469
  except_sites                     682
  Pass B summaries present         241 / 469     empty 228 (219 of them tests)

per-task block, as rendered today
  non-empty blocks                 477 of 477 modules asked
  median block                     544 chars
  blocks over max_context_chars    50  (10%)
  rows per block that are NOT derivable from the target file's own source   0
  duplicate config_read lines      24
  symbols silently cut by [:20]    296 across 28 modules
```

### Do

1. Compute every line above from `artifact.json` alone.
2. Import `build_collect_context_block` and render the block for **every**
   module, so the block section is measured, not estimated. This is the one
   import from `tools/` the script is allowed.
3. `--json` writes a flat dict; every key is stable across runs so a later diff
   is a plain key-by-key comparison.
4. Print the collect-dir path, the artifact's `collector_version` and its mtime
   in the header. The 469-vs-483 confusion above is exactly what that prevents.

### Acceptance

- [ ] Runs against any repo with a `.collect/`, changes nothing on disk.
- [ ] Two consecutive runs produce byte-identical JSON.
- [ ] Runs against an absent/corrupt `.collect/` with a clear message, exit 1.
- [ ] `docs/collect-epics/baseline.json` committed, produced by this script.
- [ ] The table above is regenerated from the script and replaces the
      hand-measured numbers in `INDEX.md` and in EPIC A/B/C.
- [ ] New: `tests/test_collect_metrics_script.py` against the mini fixture repo.

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
