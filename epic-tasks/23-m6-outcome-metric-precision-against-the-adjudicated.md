# M6 — Outcome metric: precision against the adjudicated corpus

**Severity:** MEDIUM  
**File:** `scripts/`  
**Symbol:** `—`  
**Round:** 23 of 27  
**Size:** M  
**Source:** `docs/collect-epics/EPIC-M-metrics.md` § M6  
**Depends on:** M3, and whatever EPIC B ends up shipping  
**Also touches:** `validate1/`  

---

**Priority:** Medium · **Size:** M · **Files:** `scripts/` + `validate1/`
**Depends on:** M3, and whatever EPIC B ends up shipping

Tier 3. The project already has the hard part: `validate1/truth.csv` is 54
findings adjudicated to FALSE / REAL / FIXED, and `ANALYTICS-RUNBOOK.md`
describes how it was built.

### Do

1. Re-run the improvement-list pass over the same tree with collect on and off.
2. Score both lists against `truth.csv` with the existing
   `scripts/truth_consensus.py` / `merge_validations.py` path — do not write a
   third scorer.
3. Report the number that matters:

```
                         collect off   collect on
findings produced               ___          ___
  TRUE (matches truth=REAL)     ___          ___     ← must not drop
  FALSE                         ___          ___     ← want down
  noise floor (FALSE share)     ___%         ___%    baseline 45/54 = 83%
```

4. A drop in TRUE findings is a **regression**, no matter how much the noise
   floor improves. Say so in the report, not in a footnote.

### Acceptance

- [ ] Noise floor recorded before and after, produced by the existing scorers.
- [ ] Zero REAL findings lost.
- [ ] Numbers land in `docs/collect-epics/METRICS.md` under **Measured**.

---

## Measured

Fill in as tickets land. `baseline` is produced by M1 and committed; every
later column is the same script after the named epic.

| # | metric | baseline | after A | after B | after C |
|---|---|---|---|---|---|
| 0 | LLM calls, `--collect`, 1 file changed | 483 | | | |
| 0 | LLM calls, `--collect --refresh`, 1 file changed | 1 | | | |
| 0 | summaries surviving `--collect --no-llm` | 0 / 469 | | | |
| 1 | redundant share of block chars | ~100% | | | |
| 1 | mean non-derivable rows per block | 0.008 | | | |
| 1 | blocks over budget | 50 / 477 | | | |
| 1 | modules with a Pass B purpose | 241 / 469 | | | |
| 1 | Pass C claims dropped | 1368 / 2238 (61%) | | | |
| 2 | probe misses per run | | | | |
| 2 | Gate-2 attempts per task | | | | |
| 2 | Gate-1 suppressed by static facts | 0 | | | |
| 3 | noise floor on the adjudicated corpus | 83% | | | |
| 3 | REAL findings retained | 6 / 6 | | | |

### Ordering

```
M1 ─┬─ M2 ──────────────► EPIC A can start
    ├─ M3 ──────────────► EPIC B's scope is decided by its result
    └─ M4 ─── M5 ───────► every "it works better" claim after this point
                 └─ M6 ─► the one claim that needs ground truth
```

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
