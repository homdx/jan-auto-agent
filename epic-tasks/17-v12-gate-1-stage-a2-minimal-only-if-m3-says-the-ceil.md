# V12 — Gate-1 Stage A2, minimal — **only if M3 says the ceiling is > 0**

**Severity:** MEDIUM  
**File:** `tools/auto/gate1_filter.py`  
**Symbol:** `—`  
**Round:** 17 of 27  
**Size:** S  
**Source:** `docs/collect-epics/PLAN-v2.md` § V12  
**Depends on:** M3  

---

**Priority:** decided by M3 · **Size:** S · **Files:** `tools/auto/gate1_filter.py`
**Depends on:** M3 *(v1: B1 + B2 + B4, collapsed)*

`tools/collect/bughunt_filter.py` is 173 lines of finished, provenance-isolated
code with zero callers, and `use_in_bughunt` has been parsed and ignored since
COLLECT-22. The natural home is Gate 1, between existence and the LLM presence
check:

```
Stage A   existence            (no LLM)   unchanged
Stage A2  already-safe         (no LLM)   NEW
Stage B   problem-presence     (1 LLM)    unchanged
Stage C   deduplication        (no LLM)   unchanged
```

**Scope, deliberately small.** v1 filed this as a new module with a four-rule
resolution ladder, range algebra and a tallies ticket. Measured, the whole
addressable surface is ~144 locations. So:

1. **Line citations only.** `line_start` set → ask `bughunt_filter.suppress()`
   for `f"{file}:{line_start}"`. `line_start` + `line_end` → ask for each line;
   suppress only if at least one answers `safe=True` and none answers
   `unguarded`. Symbol-only citations are **not** resolved — the index has no
   methods and the real corpus is method-level, so that path cannot work today
   (see V10's note).
2. No new module. ~40 lines in `gate1_filter.py`, calling the existing
   `bughunt_filter.suppress()`. Do not reimplement `is_safe` logic.
3. A suppressed candidate becomes a `FilterResult` with `stage="already_safe"`,
   carrying the verdict's `reason` and `detail` so the log names *which* fact
   suppressed it — not just "suppressed".
4. Gated on `[collect] use_in_bughunt`, default `false`. Flip to `true` in
   `agents_128k.ini` in the same commit **only** if M3 showed zero REAL/FIXED
   findings suppressed; otherwise it ships off and the flag comment says why.
5. Use the bridge the controller already built (`_get_collect_bridge`); never
   build a second one. Expose `CollectBridge.model` or a narrow `already_safe()`
   passthrough — reaching into `bridge._model` from another module is not
   acceptable.
6. The stage tally line comes from M4, not from here.

**Acceptance**

- [ ] `use_in_bughunt = false` → byte-identical Gate-1 behaviour and identical
      LLM call count to today.
- [ ] `true` + absent model → identical to `false` (fail-open).
- [ ] A candidate citing a guarded line is rejected at `already_safe` and never
      reaches Stage B — the presence-check call count drops.
- [ ] The rejection log names the guard.
- [ ] The M3 corpus re-run after this lands still suppresses zero REAL/FIXED.
- [ ] New: `tests/test_gate1_stage_a2_already_safe.py`.

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
