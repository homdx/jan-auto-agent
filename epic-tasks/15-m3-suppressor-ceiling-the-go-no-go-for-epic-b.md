# M3 — Suppressor ceiling: the go/no-go for EPIC B

**Status:** open (verified against `2f9005d`, 2026-09-12)  
**Severity:** CRITICAL  
**File:** `scripts/collect_metrics.py`  
**Symbol:** `—`  
**Round:** 15 of 27  
**Size:** M  
**Source:** `docs/collect-epics/EPIC-M-metrics.md` § M3  
**Depends on:** M1. **Blocks:** every ticket in EPIC B except the Pass C fix.  

---

**Priority:** Highest · **Size:** M · **Files:** `scripts/collect_metrics.py`
or a sibling script
**Depends on:** M1. **Blocks:** every ticket in EPIC B except the Pass C fix.

**Run this before writing a line of Stage A2.** EPIC B builds a suppression
path; this ticket measures how many things it could ever suppress. If the
answer is near zero, the epic shrinks to its Pass C fix and the rest is not
built.

### The arithmetic that prompted this ticket

`AlreadySafeIndex.query` answers `safe=True` from exactly three sources.
Counted on the current artifact:

| source | locations that can answer safe | note |
|---|---|---|
| guard | **23** | 2620 locations are UNGUARDED, 3 ambiguous |
| fail-open | 117 | only 22 carry a rationale |
| contract | 4 | four in the whole repository |
| **total** | **~144** | out of 2646 indexed access locations (5.4%) |

So Stage A2 can only ever fire on a candidate citing one of ~144 `path:line`
locations. That is a small lever, and EPIC B currently spends eight tickets and
a new module on it.

### Do

1. Load the historical corpus that already exists: `validate1/truth.csv`
   (54 adjudicated findings — 45 FALSE, 6 REAL, 3 FIXED) and the confirmed
   defects in `GROUND-competition.md`.
2. For each finding, try to resolve it to `path:line` and ask
   `bughunt_filter.suppress()`. Report:

```
findings                                  54
  module present in the artifact          51
  resolvable to a line at all             ___
  answered safe=True  (would suppress)    ___     ← the ceiling
    of which truth=FALSE (a real win)     ___
    of which truth=REAL or FIXED          ___     ← must be 0
```

3. Report the same for the shape the corpus is actually in. Measured already,
   and it is the fact that decides EPIC B's design:

```
findings written as path::Symbol.member   35 / 54
full dotted symbol present in the index    16 / 54
head symbol (class or function) present    49 / 54
methods in the index                        0
```

   The corpus is method-level. The index is top-level-only. Symbol-only
   resolution therefore cannot land on a method's line without EPIC C4's
   signature work or a method-level index — which C4 explicitly puts out of
   scope.

### The decision this ticket forces

| ceiling | what happens to EPIC B |
|---|---|
| 0 suppressible findings | build only the Pass C fix. Delete the rest of B. |
| 1–5 | build Stage A2 as ~40 lines inside `gate1_filter.py`, line-citations only. No new module, no range rules, no tallies epic. |
| >5 | v1's EPIC B is justified as written; build B1 with the full resolution ladder. |

### Acceptance

- [ ] The ceiling number is recorded in `docs/collect-epics/baseline.json` and
      in this file under **Measured**.
- [ ] Zero REAL/FIXED findings answer safe=True. If any does, that is a defect
      in `AlreadySafeIndex`, filed before anything else proceeds.
- [ ] The decision above is written down with the number that produced it,
      **before** EPIC B starts.

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
5. **Run the two real roots, one after the other, never combined** —
   `.smoke_tests/` and `.regression_tests/` are symlink views onto `tests/`
   and run nothing extra; combining roots in one `pytest` call produces ~362
   false errors from a conftest collision:
   ```bash
   python3 -m pytest tests -q --timeout=180 && python3 -m pytest tests_bugfix -q --timeout=180
   ```
   `python` is not on PATH — use `python3`. One suite at a time — the machine
   is shared with the other round entrants.
6. **Fail open.** A broken artifact, a malformed config key or an absent model
   degrades to "no collect data" and never raises into a run. Everything added
   here inherits that.
7. **Never widen provenance.** Static facts and LLM prose stay separately
   labelled. `is_safe()` keeps reading only `guarded_accesses` /
   `FAIL_OPEN_REGISTRY` / `CONTRACTS`.
8. **Never run anything against a live provider config.** Copy
   `agents_128k.ini` to a scratch path and point `base_url` at a stub before any
   measurement run.
