# V15 — Docs sync and the consumer column

**Severity:** LOW  
**File:** `README.md`  
**Symbol:** `—`  
**Round:** 22 of 27  
**Size:** S  
**Source:** `docs/collect-epics/PLAN-v2.md` § V15  
**Depends on:** everything  
**Also touches:** `docs/`  

---

**Priority:** Low · **Size:** S · **Files:** `README.md`, `Collect.MD`,
`agents*.ini`, `docs/` **Depends on:** everything *(v1: C7 + C9)*

**Do**

1. Re-run `M1` and settle every remaining zero-reader table honestly, one word
   each: **used** (name the consumer), **human-only** (it feeds a rendered `.md`
   and that is its whole job), or **retire** (remove producer and key, bump
   `collector_version`). Candidates today: `gates` (7), `thin_coverage` (10),
   `sibling_gaps` (**0 — retire**). The nine rendered pages (~950 KB, of which
   `MODULE_MAP.md` is 725 KB) are legitimately human-only and should be labelled
   so rather than treated as an unfinished integration.
2. `Collect.MD`'s table gains a **consumer** column.
3. `README.md` `[collect]`: `pack_enabled`, `--rebuild`, `--drop-summaries`, the
   corrected `--refresh` description, and what `use_in_bughunt` / `use_in_doc`
   now do.
4. `docs/collect-epics/METRICS.md` gets the filled before/after table.

**Acceptance**

- [ ] `grep -rn "no-op today" agents*.ini` returns nothing about `use_in_*`.
- [ ] Every top-level artifact key has a documented consumer or a documented
      human-only / retired status.
- [ ] Someone reading only `README.md` can turn the pack on and know what it costs.

---

## 5. Idea ledger — where every v1 ticket went

Nothing was silently dropped.

| v1 | fate in v2 | why |
|---|---|---|
| A1 loader tables | **V1** | minus `sibling_gaps` (0 entries) |
| A2 neighbourhood API | **V1** | 3 queries instead of 5; `exclude_tests` added from data |
| A3 `FactRow` + `assemble` + new module | **V2**, as a tuple of functions | ten fixed lines do not need a generic selector |
| A4 port existing rows | **V2** | same ticket as the structure |
| A5 callers/callees/tests | **V3** | + test-file filter, + `zero_coverage` form |
| A6 guarded / fails_open | **V4**, `guarded` **cut** | 23 GUARDED locations on 15 of 469 modules |
| A7 risk / owns_config | **V4**, `owns_config` folded into `config_read` | `readers` encodes sharing, not ownership |
| A8 neighbours (llm) | **V5**, unchanged | the one Pass B consumer; design decision kept verbatim |
| A9 bridge wiring + `_shrink` guard | **V6** | unchanged, including the characterization test |
| A10 persist call graph | **appendix** | `import_edges` is good enough for the row; re-parsing 469 modules is separate work |
| A11 seven config keys | **V6**, one boolean | the other six were knobs on correct defaults |
| A12 measure | **EPIC M** (M1, M2) | measurement belongs before the work, not at the end of one epic |
| B1 resolution ladder + new module | **V12**, line citations only | corpus is 35/54 method-level; index has 0 methods |
| B2 Stage A2 | **V12** | same behaviour, ~40 lines, no new module |
| B3 two grounding notes | **V13**, one function | |
| B4 stage tallies | **M4** | it is a metric |
| B5 precision harness | **M3**, promoted to a **gate** | run it *before* building the suppressor, not after |
| B6 flip the flag | folded into **V12** step 4 | |
| B7 Pass C test citations | **V11**, unchanged | the best-value ticket in either plan |
| B8 `check_improvements` annotation | **appendix** | second surface; do it after V12 proves the first |
| C1 stale → incremental | **V7** | |
| C2 fix flag docs | **V7**, same commit | documentation of a behaviour change belongs with it |
| C3 `--no-llm` preserves | **V8**, unchanged | |
| C4 real signatures | **V10**, unchanged | |
| C5 trace events | **M4** | it is a metric |
| C6 docs mode | **V14** | |
| C7 prune unreachable | **V15**, merged | |
| C8 freshness | **V9**, unchanged | reproduced against live code |
| C9 docs sync | **V15**, merged | |

**Appendix — deliberately not scheduled.** Persist the call graph
(`graph.build_call_edges`, written, zero callers, would sharpen `calls_into`);
index methods (4030 → ~12000 symbols, and the prerequisite if Stage 3 ever needs
symbol resolution); annotate `check_improvements.py`; per-mode priority tuples
beyond code/docs. Each is a real idea with a real cost and no measured demand
yet — `M1` after Stage 1 is what would create the demand.

---

## 6. The short path

If only part of this gets built, build this part:

```
M1  baseline                      ← nothing starts without it
V1  loader keeps the import graph
V2  block becomes an ordered list
V3  callers / calls_into / tests   ← the biggest single jump in supply
V5  neighbours (llm)               ← makes 483 LLM calls a run useful
L4  graph sees `from pkg import mod as alias`   ← V3 rows lie without it
L5  test_map covers all four test roots         ← callers/tests rows disagree without it
L6  budget: fact rows shrink, public_symbols never displaces them
V7  --collect stops costing 483 calls
V9  facts stop being stale-but-labelled-fresh
V11 Pass C stops eating test summaries   ← also feeds V5
M2  measure the same numbers again
```

Eleven tickets — L4–L6 were added after V3 was measured against the artifact
(see `EPIC-L-live-findings.md`); they are collect-side defects V3 exposed, not
changes to V1–V3. Everything else — V4, V6's memo, V10, V12–V15 — is refinement on
top of a working pack, and each can be dropped without breaking the ones above.

| | v1 | v2 |
|---|---|---|
| tickets | 29 | 15 + 6 measurement |
| new modules | 2 (`factpack.py`, `gate1_safety.py`) | 0 |
| new config keys | 9 | 2 (`pack_enabled`, `auto_refresh_between_tasks`) |
| new artifact keys | 1 (`call_edges`) | 0 |
| rows in the pack | 11 | 9 |
| `_shrink` | untouched | untouched |
| baseline before the work | none | `baseline.json`, committed |

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
