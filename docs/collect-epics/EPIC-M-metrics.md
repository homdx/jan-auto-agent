# EPIC M — Measure it first, then measure it again

**Type:** Epic (mini) · **Prepared:** 2026-09-10
**Component:** `scripts/` (new), `tools/auto/run_trace.py`, `analyze_logs.py`
**Depends on:** nothing. **Blocks:** the go/no-go decision inside EPIC B.
**Applies to both plans** — the v1 epics (`EPIC-A/B/C`) and the simplified
`PLAN-v2.md`. It is the neutral yardstick the two are compared with.

---

## Why this epic exists at all

Every number in EPIC A, B and C was produced by a throwaway script in a
scratchpad that no longer exists. That is not a baseline, it is an anecdote.
Two concrete symptoms:

* The epics quote **483 modules**. The artifact on disk right now has **469**.
  The 483 came from a rebuild that swept in files that were not part of the
  subject tree. Nobody can tell which figure any given line refers to.
* `sibling_gaps` is described in A1 as a table worth recovering. It has
  **0 entries**. A1 would have shipped code to load an empty list.

So: no work in A, B or C starts until one committed script prints one committed
table. Then the same script prints it again after each epic, and the diff is the
answer to "did this help".

## The one rule

> One script. Read-only. No LLM. Deterministic. JSON out, table out.
> Its output for the current tree is committed as the baseline **before A1**.

If a metric cannot be produced that way, it belongs in Tier 2 or 3 below and
gets its own harness — not an extra flag on the static script.

---

## The four tiers

The epics improve two different things and the metrics must not blur them.

| tier | question | who it answers to |
|---|---|---|
| **0 — cost** | what does collecting the facts cost? | EPIC C |
| **1 — supply** | how many non-redundant facts reach the prompt? | EPIC A |
| **2 — behaviour** | does the agent *act* differently once they do? | A + B |
| **3 — outcome** | is the work it produces better? | the whole thing |

Tier 1 is the one that is trivially gameable — a bigger block is not a better
block. It is reported only as a ratio against Tier 2, never on its own.

Tier 2 is the honest cheap signal, and it needs no ground truth: if the pack
carries what the model was missing, the model should **ask fewer questions**.
`ArchProbe` already records requests, hits, misses, declines and escalations;
`analyze_logs.py` already parses them. A drop in probe misses and in Gate-2
attempts is the pack doing its job. A rise is the pack adding noise.

---

# Tickets

## M1 — `scripts/collect_metrics.py` — the static baseline

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

## M2 — Block redundancy: the number EPIC A exists to move

**Priority:** Highest · **Size:** S · **Files:** `scripts/collect_metrics.py`
**Depends on:** M1

The claim driving EPIC A is *"the block describes the file the coder already
has"*. Right now that is an argument. This makes it a number.

### Do

For every module, split the rendered block into lines and classify each:

* **redundant** — the line's content is derivable from the target file's own
  source (`public_symbols`, the `module:` line, `config_read` — every one of
  these is visible by reading the file in the prompt);
* **new** — it is not (`callers`, `calls_into`, `tests`, `risk`, `owns_config`,
  `fails_open`, `neighbours`, `contract` — a contract lives in a registry, not
  in the file).

Report:

```
redundant chars / total chars     ___ %      (baseline: ~100%)
blocks with >=1 new row           ___ / 477  (baseline: 4 — the contract rows)
mean new rows per block           ___        (baseline: 0.008)
```

The classification is a static dict of `kind -> redundant|new` in the script.
It does not have to be clever; it has to be fixed, so before and after are
comparable.

### Acceptance

- [ ] Baseline recorded: redundant share, blocks with a new row, mean new rows.
- [ ] After EPIC A the same three numbers are recorded in the same table.
- [ ] The kind→class mapping lives in one dict with a comment saying why each
      kind is classified the way it is.

---

## M3 — Suppressor ceiling: the go/no-go for EPIC B

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

## M4 — Runtime counters: collect events + Gate-1 stage split

**Priority:** High · **Size:** M · **Files:** `tools/auto/run_trace.py`,
`tools/auto/collect_bridge.py`, `tools/auto/gate1_filter.py`, `analyze_logs.py`

Tier 0 and Tier 1 are static. Tier 2 needs the run to say what happened.
(This ticket absorbs v1's C5 and B4, which were measurement work filed as
feature work at the end of two different epics.)

### Do

1. Four collect events, structured like the existing ones:

   | event | fields |
   |---|---|
   | `collect_block` | `task_id`, `target_file`, `chars`, `rows_kept`, `rows_cut` |
   | `collect_shrink` | `task_id`, `target_file`, `before`, `after`, `path` = `llm` \| `truncate` |
   | `collect_miss` | `task_id`, `target_file`, `reason` = `absent` \| `stale` \| `dirty` \| `unknown_module` |
   | `collect_summary` | per run: blocks injected, chars injected, shrink calls, misses |

   `collect_shrink` observes `_shrink` **from the outside** — from `context_for`,
   by comparing lengths and reading the existing `shrink_calls`. It does not
   modify `_shrink`.

2. `plan_phase: gate1 accepted=N rejected=M` gains the per-stage split
   (`existence=… already_safe=… presence=… duplicate=…`). With the feature off
   the line still prints `already_safe=0`, so log parsing is stable across the
   flag.

3. `analyze_logs.py` prints a **collect** section next to its existing probe
   section:

```
collect   blocks 12/14 tasks · 6 431 chars · shrink 1 (llm) · miss 2 (dirty)
probe     requests 9 · hits 7 · misses 2 · declined 0 · escalated 1
gate1     accepted 4 · existence 3 · already_safe 0 · presence 5 · duplicate 1
```

### Acceptance

- [ ] A `--dry-run` against a stub emits all four events plus the summary.
- [ ] `git diff` touches no line inside `CollectBridge._shrink`.
- [ ] Zero events and an unchanged log shape when no bridge is wired in.
- [ ] New: `tests/test_run_trace_collect_events.py`.

---

## M5 — A/B harness: one goal, two runs, one diff

**Priority:** High · **Size:** M · **Files:** `scripts/collect_ab.sh` or
`scripts/collect_ab.py`
**Depends on:** M4

The only way to claim "the agent works better" is to run the same thing twice.

### Do

1. One goal, one base tree, fixed seed, `--dry-run`, against the local stub —
   run A with `pack_enabled = false`, run B with `pack_enabled = true`.
   Everything else identical, including the config file, which the script
   copies and patches rather than editing in place.
2. Diff the Tier-2 counters:

```
                          pack off   pack on   delta
probe requests                  ___       ___
probe misses                    ___       ___     ← want down
context re-requests             ___       ___     ← want down
gate1 rejected: existence       ___       ___     ← want down (fewer phantom paths)
gate1 rejected: presence        ___       ___
gate2 attempts per task         ___       ___     ← want down
tasks blocked                   ___       ___
prompt chars per coder call     ___       ___     ← want up only a little
LLM calls, whole run            ___       ___
```

3. The pass condition is deliberately weak and stated up front: **probe misses
   and Gate-2 attempts do not increase, and prompt size grows by less than the
   pack's own budget.** A pack that makes the model ask *more* questions is a
   pack that added noise, and that is the failure this harness exists to catch.

### Acceptance

- [ ] Reproducible: two runs of A produce the same counters.
- [ ] Never touches `agents_128k.ini` — copies to a scratch path and patches
      the copy. (Do not point a measurement run at a live provider config.)
- [ ] Output committed alongside the baseline JSON.

---

## M6 — Outcome metric: precision against the adjudicated corpus

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
