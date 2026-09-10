# EPIC B — Close the bug-hunting loop

**Type:** Epic
**Component:** `tools/collect/bughunt_filter.py` → `tools/auto/gate1_filter.py`,
`tools/auto/gate1_grounding.py`, `tools/collect/verifier.py`
**Prepared:** 2026-09-10
**Depends on:** A1–A3 for B3 only; B1/B2/B7 are independent of EPIC A

---

> **Numbers re-measured — M1** (`scripts/collect_metrics.py`, 2026-09-10).
> The suppressor-ceiling arithmetic was written against a rebuilt 483-module
> artifact. The artifact on disk says: 469 modules, 2814 `guarded_accesses`
> collapsing to 2646 distinct locations, of which 23 are `GUARDED` and 3 are
> ambiguous (2620 all-UNGUARDED), 117 `fail_open_registry` entries with 22
> carrying a rationale, 4 contracts, 0 methods indexed. The ceiling is
> unchanged in shape — about 144 of 2646 locations (5.4%) can answer
> `safe=True` — because none of its three inputs moved. Full table:
> [`baseline.json`](baseline.json).

## Problem

`tools/collect/bughunt_filter.py` is 173 lines of finished, documented,
provenance-isolated code with **zero callers anywhere in the repository**:

```
$ grep -rn "bughunt_filter" --include='*.py' . | grep -v tests
tools/collect/bughunt_filter.py:1:   (its own module docstring)
tools/collect/cli.py:144:            use_in_bughunt: bool = False
tools/collect/cli.py:185:            use_in_bughunt=_get_bool(config, "use_in_bughunt", False),
```

`use_in_bughunt` is parsed into `CollectSettings` and then never consulted. The
module was built against a `/bughunt` command that this codebase does not have.

Meanwhile the *actual* bug-hunting surface in this codebase — the one that
generates "fix the error handling in X" tasks — is the Architect → Gate 1
pipeline, and Gate 1 spends **one LLM call per candidate** deciding whether the
problem is real, with no access to the static facts that already answer the
question for free.

Consequences, both measured:

- `is_safe()` — the single query over 2814 `guarded_accesses`, 117
  `fail_open_registry` entries and the contracts table — is called from nowhere
  outside `tools/collect/`.
- Pass C drops **1368 of 2238** Pass B claims (61%), and 228 modules end with an
  empty `purpose`. **219 of those 228 are test files** — a test naming the symbol
  it tests is scored as a cross-module hallucination.

## Approach

Give Gate 1 a **Stage A2: already-safe check** — deterministic, no LLM, between
today's Stage A (existence) and Stage B (LLM presence check). A candidate whose
cited line is already known guarded / fail-open / contract-covered is suppressed
*before* it costs a model call. Everything ambiguous falls through to Stage B
exactly as today.

```
Stage A   existence check          (no LLM)      unchanged
Stage A2  already-safe check       (no LLM)      NEW — bughunt_filter.suppress()
Stage B   problem-presence check   (1 LLM call)  unchanged, but sees a safety note
Stage C   deduplication            (no LLM)      unchanged
```

Fail-closed throughout: suppression requires a **positive** static fact.
`unguarded`, `unknown` and `ambiguous_location` all mean "keep the candidate".

---

# Tickets

## B1 — `CitedLocation` → `BughuntCandidate` resolution

**Priority:** High · **Size:** M · **Files:** new
`tools/auto/gate1_safety.py`

`bughunt_filter.suppress()` wants `location` as `"path/to/module.py:line"`,
matching `GuardedAccess.location`. Gate-1 candidates carry a `CitedLocation`
with `file`, `symbol`, `line_start`, `line_end` — often with `line_start=None`
and only a symbol.

This ticket is the adapter, and it is where the safety of the whole epic lives.

### Resolution rules — in order

1. **`line_start` set, `line_end` unset** → one candidate location,
   `f"{file}:{line_start}"`. Eligible for suppression.
2. **`line_start` and `line_end` set** → the whole range. Suppress **only if
   every line in the range that has an indexed access answers `safe=True`**, and
   at least one does. One unguarded access in the range keeps the candidate.
3. **Symbol only, no line** → resolve the symbol's `lineno` from the collect
   model (100% of the 4093 symbols in the artifact carry one), then treat as a
   *range* from that line to the next top-level symbol's `lineno - 1`. Same
   all-or-nothing rule as (2).
4. **Neither symbol nor line** → not eligible. Gate 1's Stage A already rejects
   these (`cited_location lacks file and/or anchor`).

### Why all-or-nothing

`AlreadySafeIndex.query` already refuses to answer for a line with disagreeing
accesses (`ambiguous_location`, `safe=False`) precisely so a static fact never
vouches for something it does not cover. Extending that to ranges is the same
principle one level up: suppressing a whole candidate because one line in its
range is guarded would let a guarded access vouch for an unguarded sibling.

### Do

```python
def candidate_locations(cited, model) -> List[str]:
    """Every `path:line` this candidate cites, or [] when it cites none."""

def is_candidate_already_safe(cited, model, *, root=None) -> Optional[SuppressionVerdict]:
    """One verdict for the whole candidate, or None when it is not eligible.

    Returns a suppressing verdict only when every eligible location answers
    safe=True and at least one location was eligible. Never raises.
    """
```

Reuse `bughunt_filter.suppress()` for the per-location call — do not
reimplement `is_safe` logic here.

### Acceptance

- [ ] Symbol-only citation resolves to the symbol's real line span.
- [ ] A range with one unguarded indexed access is **not** suppressed.
- [ ] A range with no indexed access at all is **not** suppressed (`unknown`).
- [ ] Absent/stale model → `None` for everything, no exception.
- [ ] New: `tests/test_gate1_safety_resolution.py`, including the
      "one guarded, one unguarded on the same line" case from
      `AlreadySafeIndex.query`'s own docstring.

---

## B2 — Gate 1 Stage A2: wire the suppressor

**Priority:** High · **Size:** M · **Files:** `tools/auto/gate1_filter.py`
**Depends on:** B1

### Do

1. `Gate1Filter.filter()` runs Stage A2 between existence and presence.
2. A suppressed candidate becomes a `FilterResult` with a **new**
   `stage="already_safe"` alongside today's `"existence"` / `"presence"` /
   `"duplicate"`, carrying `SuppressionVerdict.reason` and `.detail` so the log
   line explains *which* static fact suppressed it.
3. Gated on `[collect] use_in_bughunt` — the key that has been parsed and
   ignored since COLLECT-22. Default stays `false`; B6 flips it after B8 proves
   the precision.
4. The bridge is the one already built per run
   (`AutoController._get_collect_bridge`) — never build a second
   `CollectBridge`; `make_collect_bridge`'s build-once contract and
   `tests/test_collect_bridge_wiring.py::test_collect_model_loaded_once_per_run`
   both hold.
5. Expose `CollectBridge.model` (or a narrow `already_safe(...)` passthrough) —
   Stage A2 needs the `CollectModel`, and reaching into `bridge._model` from
   another module is not acceptable.

### Acceptance

- [ ] `use_in_bughunt = false` → byte-identical Gate-1 behaviour and identical
      LLM call count to today.
- [ ] `use_in_bughunt = true` with an absent model → identical behaviour to
      `false` (fail-open).
- [ ] A candidate citing a guarded line is rejected at `stage="already_safe"`
      and **never reaches Stage B**, i.e. the presence-check call count drops.
- [ ] The rejection log names the guard, not just "suppressed".
- [ ] New: `tests/test_gate1_stage_a2_already_safe.py`.

---

## B3 — Safety facts in Gate-1 grounding notes

**Priority:** Medium · **Size:** S · **Files:** `tools/auto/gate1_grounding.py`,
`tools/auto/collect_bridge.py`
**Depends on:** A6 (row rendering), B1

Gate 1 already builds two grounding notes for the Stage-B prompt —
`collect_contract_note` and `existing_test_coverage_note`. Add the two that
matter most for a "the problem is already handled" verdict.

### Do

```python
def already_guarded_note(collect_bridge, cited) -> Optional[str]:
    """'Static analysis records this access as already guarded by <guard>.'"""

def fail_open_note(collect_bridge, cited) -> Optional[str]:
    """'This site fails open deliberately: <exception> — <rationale>.'
    Rendered only when a rationale exists; an undocumented fail-open is not
    evidence of intent and must not be presented as if it were."""
```

Both need `CollectBridge` accessors mirroring `contracts_for_symbol` /
`tests_covering`:

```python
def guards_at(self, file_path: str, line: int) -> Tuple[GuardedAccess, ...]: ...
def fail_open_at(self, file_path: str, line: int) -> Tuple[FailOpenEntry, ...]: ...
```

These are for the **ambiguous** candidates — the ones B2 deliberately does not
suppress. B2 removes the certain false positives; B3 gives the model the
evidence to judge the uncertain ones.

### Acceptance

- [ ] Undocumented fail-open (`rationale is None`) produces no note.
- [ ] Notes appear in the Stage-B prompt and are absent when the model is
      absent/stale.
- [ ] New: `tests/test_gate1_grounding_safety_notes.py`.

---

## B4 — `already_safe` in the rejection tallies

**Priority:** Medium · **Size:** S · **Files:** `tools/auto/gate1_filter.py`,
`analyze_logs.py`
**Depends on:** B2

A suppression path nobody can count is a suppression path nobody can trust.

### Do

1. `plan_phase: gate1 accepted=N rejected=M` gains the per-stage split:
   `existence=… already_safe=… presence=… duplicate=…`.
2. `analyze_logs.py` learns the new stage and reports it per run.
3. Report the LLM calls **saved**: one Stage-B call per candidate suppressed at
   A2 — the direct cost argument for turning the feature on.

### Acceptance

- [ ] A run with suppressions prints the split and the saved-call count.
- [ ] A run with `use_in_bughunt = false` prints the same line with
      `already_safe=0`, so log parsing is stable across the flag.
- [ ] New: `tests/test_gate1_stage_tallies.py`.

---

## B5 — Precision harness for the suppressor

**Priority:** High · **Size:** M · **Files:** `tests/` + a small script
**Depends on:** B2

**Do not turn this feature on without this ticket.** A suppressor that hides a
real bug is worse than the false positives it removes.

### Do

1. Assemble a fixed corpus of candidates against the real
   `jan-to-fix-pull-v2` artifact: every candidate the Architect produced in the
   last recorded runs (`logs/`, `full-log-opus.txt`, `super2.txt` all carry
   them), plus the confirmed-real defects from `GROUND-competition.md` and
   `validate1/truth.csv`.
2. Run Stage A2 over the corpus and report:
   ```
   candidates            ___
   suppressed            ___   (___%)
   suppressed by reason  guarded ___ · fail_open ___ · contract ___
   GROUND-truth defects suppressed   ___     ← must be 0
   ```
3. **The gate is the last line.** Any confirmed-real defect from the ground file
   being suppressed is a hard stop: fix the resolution rules in B1 before
   proceeding.

### Acceptance

- [ ] Harness is reproducible and read-only.
- [ ] Zero ground-truth defects suppressed.
- [ ] Numbers recorded in the commit message and in
      `docs/collect-epics/EPIC-B-bughunt.md` under a "Measured" heading.

---

## B6 — Flip `use_in_bughunt` on by default

**Priority:** Medium · **Size:** S · **Files:** `agents*.ini`, `README.md`
**Depends on:** B5

Only after B5 shows zero ground-truth suppressions.

### Do

- `use_in_bughunt = true` in `agents_128k.ini` first; the other profiles follow
  in the same commit only if the harness was run against each.
- Update the `[collect]` comment block: it currently says these flags "stay
  false until the corresponding EPIC G loader integration lands, so turning
  these on ahead of that is a no-op today". After B2 that sentence is false for
  `use_in_bughunt` and must be rewritten to describe what it now does.
- `README.md`'s `[collect]` section gains the Stage A2 description.

### Acceptance

- [ ] A full `--auto --dry-run` against a stub shows a non-zero `already_safe`
      count and a reduced Stage-B call count versus the same run with the flag
      off.
- [ ] No `agents*.ini` claims a behaviour it no longer has.

---

## B7 — Pass C: stop eating test-file summaries

**Priority:** High · **Size:** M · **Files:** `tools/collect/verifier.py`
**Depends on:** nothing

### The defect

Pass C drops a claim whose cited symbol belongs to a module other than the one
being summarized. For a test file that rule is inverted from reality: a test's
entire purpose is to name symbols from the module under test.

Measured on the real artifact:

```
claims extracted            2238
dropped                     1368  (61%)
  dropped:no-citation             1181
  dropped:sibling-citation-failed  187
modules with ≥1 drop         355 / 483
modules left with no purpose 228
  of which test files        219      ← this ticket
  of which source files        9
top offenders  tests_bugfix/test_bugfix_c2_architect_probe_config_guard.py (61 drops)
               tests_bugfix/test_bugfix_c1_gate_registry_profile_guard.py  (43)
               tests/test_auto_c1.py                                       (26)
```

### Do

1. In `verify_repo`, when the module under verification is a test file
   (`tests/`, `tests_bugfix/`, `.smoke_tests/`, `.regression_tests/`, or
   `test_*.py` / `*_test.py` anywhere), extend the set of citable symbols from
   "symbols defined in this module" to "symbols defined in this module **plus
   every module this test imports**".
2. `import_edges` already knows what a test imports — no new analysis.
3. Everything else about Pass C is unchanged. A test citing a symbol from a
   module it does **not** import is still a hallucination and still drops.
4. Do not special-case by filename inside the claim extractor; make it a
   parameter of the verification call so the rule is visible and testable.

### Acceptance

- [ ] Re-running a full `--collect` on `jan-to-fix-pull-v2` leaves ≤20 modules
      with an empty purpose (from 228); record the exact number.
- [ ] A test citing a symbol from a module it does not import still drops.
- [ ] A non-test module's rules are unchanged — the 9 source-file drops are out
      of scope here and stay dropped.
- [ ] New: `tests_bugfix/test_collect_verifier_test_file_citations.py`.

---

## B8 — Wire the suppressor into `check_improvements.py`

**Priority:** Low · **Size:** M · **Files:** `check_improvements.py`
**Depends on:** B1

The second real bug-hunting surface in this repo is the improvement-list
checker, which is exactly where the "add error handling to X" false positives
are read by a human.

### Do

1. Where a finding carries a `file:line`, run it through
   `bughunt_filter.suppress()` and annotate — **annotate, do not delete**. A
   human reading a list wants to see "suppressed: already guarded by
   `if stack:` at :212", not a shorter list.
2. Gate on the same `use_in_bughunt`.
3. Add a `--no-collect` escape hatch for auditing the raw list.

### Acceptance

- [ ] Annotation only; no finding disappears from the report.
- [ ] Absent artifact → the tool behaves exactly as today.
- [ ] New: `tests/test_check_improvements_collect_annotation.py`.

---

## Measured

_(fill in as tickets land — B5 and B7 both produce numbers this epic is judged on)_

| metric | baseline | after |
|---|---|---|
| Gate-1 candidates suppressed by static facts | 0 (no callers) | |
| Stage-B LLM calls saved per run | 0 | |
| ground-truth defects wrongly suppressed | — | must be 0 |
| modules with an empty Pass B purpose | 228 (219 tests) | |
| Pass C claims dropped | 1368 / 2238 (61%) | |
