# COLLECT-USE — make the collected facts actually reach the model

**Prepared:** 2026-09-10
**Component:** `tools/collect/` (producer) → `tools/auto/` (consumers)
**Branch:** `competition`
**Basis:** traced audit of `--collect` / `--collect --refresh` against
`../jan-to-fix-pull-v2` (483 modules), run entirely against a local stub LLM.

---

## Two plans and one yardstick

| file | what it is |
|---|---|
| [`EPIC-M-metrics.md`](EPIC-M-metrics.md) | **read first.** The measurement mini-epic: one committed script, one committed baseline, the same numbers again afterwards. Applies to both plans and is the only way to compare them. `M3` decides whether EPIC B gets built at all. |
| `EPIC-A/B/C` (below) | plan **v1** — 29 tickets, written from the reachability trace. **Superseded**; kept for diffing, not for implementing. |
| [`PLAN-v2.md`](PLAN-v2.md) | plan **v2** — the same analysis, 15 tickets, no new modules, no new config ladder. Includes a ticket-by-ticket ledger of where every v1 idea went, and the measured evidence for each simplification. |
| [`LIVE-RUN-VALIDATION.md`](LIVE-RUN-VALIDATION.md) | the plans checked against **five live `--auto` runs** (2026-09-09 22:10 UTC), not against a harness. One premise turned out understated, one live defect surfaced, one run was producing competition data blind. |
| [`EPIC-L-live-findings.md`](EPIC-L-live-findings.md) | `L1`–`L3` — the three tickets that measurement produced. Additive; neither plan is rewritten. |
| [`RUN-THE-EPIC-COMPETITION.md`](RUN-THE-EPIC-COMPETITION.md) | **how to actually run these**: N agents, one ticket per round, merge the winner, next round from the merged tree. Reuses the fix-round machinery unchanged. |
| [`MEASURE-BEFORE-AFTER.md`](MEASURE-BEFORE-AFTER.md) | **the before/after protocol**: which of the three instruments decides what, the exact re-run commands, and which columns are a verdict versus which are weather. Read with `EPIC-M-metrics.md`. |

Both plans keep `CollectBridge._shrink` byte-identical. v2 is the recommended
path; v1 is kept so the two can be diffed.

**To run a round:** `python3 scripts/split_epic_tickets.py` turns v2 + M + L
into `epic-tasks/NN-*.md` (24 tickets, in dependency order) in the exact shape
`scripts/next_task.py` already reads, then follow
[`RUN-THE-EPIC-COMPETITION.md`](RUN-THE-EPIC-COMPETITION.md).
`scripts/judge_epic_round.py` scores a round's worktrees on the mechanical
facts — `_shrink` untouched, one commit, nothing pushed, a test shipped.

### The numbers in this file and in EPIC A/B/C are script-produced

`M1` landed the yardstick: `scripts/collect_metrics.py` is one read-only pass
over `.collect/artifact.json` — no repo scan, no LLM, no network — and it
prints a table and writes a flat JSON whose keys are stable across runs, so a
later epic's baseline is a plain key-by-key diff. The baseline for the artifact
on disk is committed as [`baseline.json`](baseline.json):

```bash
python3 scripts/collect_metrics.py --collect-dir .collect \
    --json docs/collect-epics/baseline.json
```

Its header prints the collect dir, the artifact's `collector_version` and its
mtime, which is what keeps a 469-vs-483 ambiguity from recurring silently.

Re-measured against the artifact on disk, four of v1's design decisions do not
survive — `sibling_gaps` is empty (0 entries), only 23 of 2646 access locations
are `GUARDED`, `config_map.readers` encodes sharing rather than ownership, and
the adjudicated finding corpus is method-level while the symbol index has no
methods (`methods_indexed: 0`). See `PLAN-v2.md` §2. What `M1` cannot measure
yet is left hand-measured and named as such: everything in the M2–M4 columns of
EPIC M's **Measured** table.

## Why these epics exist

The producer is sound. The consumer side is not wired to it.

Traced with an `__getattribute__` recorder on `CollectModel` / `ModuleRecord` /
`FunctionRecord`, then driving every consumer entry point
(`context_for`, `pull_symbol`, `module_symbols`, `contracts_for_symbol`,
`tests_covering`), the 2.37 MB artifact splits like this:

| fate | bytes | what |
|---|---|---|
| reachable by an `--auto` prompt | 0.91 MB (38%) | `path`, `public_symbols`, `config_reads`, `parse_error`, `contracts`, `test_map` |
| never read by anything | 1.16 MB (48%) | `imports`, `except_sites`, `guarded_accesses`, `language`, **every Pass B `summary`** |
| loaded, no consumer | 0.16 MB (6%) | `risk_index`, `config_map`, `fail_open_registry`, `gates`, `zero_coverage`, `thin_coverage` |
| dropped by the loader | 0.13 MB (5%) | `import_edges`, `imported_by`, `entry_points`, `sibling_gaps` |

Two structural facts drive every ticket below:

1. **The per-task block describes the file the coder can already read.**
   `Coder._build_prompt` calls `_read_file_contents(target_files)` — the full
   source of the target file is in the prompt. `build_collect_context_block`
   then spends its budget listing that same file's qualnames (median block:
   544 chars, 477 of which are a `public_symbols` line). Nothing in the block
   describes what the coder *cannot* see.
2. **Pass B is the only network cost in the pipeline and no consumer reads it.**
   483 LLM calls per full build; `ModuleRecord.summary` is read only by
   `render.py` (MODULE_MAP.md, for humans) and `verifier.py` (which checks it).

Plus three implemented-but-unreachable producers:
`tools/collect/bughunt_filter.py` (173 lines, zero callers),
`graph.build_call_edges` (zero callers, not persisted),
and `use_in_bughunt` / `use_in_doc` (read into settings, never acted on).

And one contract that does not hold. `collect_bridge.py` promises that a stale
model is treated exactly like an absent one — but `status` is computed once at
run start and the bridge is cached for the whole `Controller`. Since `--auto`
edits and commits source files, every task after the first is served pre-edit
facts still labelled `fresh`. Reproduced against the live code; ticket **C8**.

---

## The epics

| # | epic | what it lands | tickets |
|---|---|---|---|
| A | [Neighbourhood Fact Pack](EPIC-A-fact-pack.md) | the per-task block carries what the coder cannot see: callers, callees, tests, guards, fail-opens, risk, config owners, neighbour purposes | A1–A12 |
| B | [Close the bug-hunting loop](EPIC-B-bughunt.md) | `bughunt_filter` wired into Gate 1 as a no-LLM Stage A2; Gate-1 grounding notes gain safety facts; Pass C stops eating test summaries | B1–B8 |
| C | [Producer cost, honesty, observability](EPIC-C-producer.md) | `--collect` stops costing 483 LLM calls per changed file; `--no-llm` stops destroying Pass B; real signatures; collect telemetry; docs mode switched on | C1–C9 |

Read them in that order. A is the architecture; B and C are each independently
shippable once A1–A3 exist.

---

## Hard constraint carried by every ticket

> **`CollectBridge._shrink` is not to be modified, reordered, or replaced.**

It was written and debugged over a long stretch and stays exactly as it is,
including its overshoot tolerance, its logging and its hard-truncation
fallback. The new assembly runs **before** it, not instead of it:

```
FactPack.assemble(...)          # NEW — priority-ordered, budget-aware
        ↓
   block text
        ↓
len(block) <= max_context_chars_auto ?
        ↓ no                          ↓ yes
CollectBridge._shrink(block)     return block
   (UNCHANGED, today's code)
```

Because the pack targets the budget by construction, `_shrink` fires rarely
instead of on 10% of blocks — but it stays reachable (a single oversized row —
a long contract description, a long neighbour purpose — still overshoots) and
byte-identical. **A9** adds a characterization test pinning its current
behaviour so no later ticket can drift it.

---

## Working these

House rules, same as every previous round:

1. **Verify before implementing.** Grep the live source for what the ticket
   claims. These tickets were written against `competition` at `68b78a0`;
   if the code has moved, the ticket is the stale one.
2. **One local commit per ticket.** Never push.
3. **Every ticket ships a test.** New behaviour → `tests/`; a fix to something
   that used to be wrong → `tests_bugfix/` (repo convention).
4. **Run the two real roots, one after the other, never combined** —
   `.smoke_tests/` and `.regression_tests/` are symlink views onto `tests/`;
   combining roots in one `pytest` call produces ~362 false errors from a
   conftest collision:
   ```bash
   python3 -m pytest tests -q --timeout=180 && python3 -m pytest tests_bugfix -q --timeout=180
   ```
   `python` is not on PATH — use `python3`.
5. **Every new consumer is fail-open.** The existing contract — a broken
   artifact, a malformed config key, an absent model degrade to "no collect
   data" and never raise into a run — extends to everything added here.
6. **Never widen provenance.** Static facts and LLM prose stay separately
   labelled in the pack. `is_safe()` must keep reading only
   `guarded_accesses` / `FAIL_OPEN_REGISTRY` / `CONTRACTS`.

### Measuring it

Two numbers decide whether these epics worked. Both are script-produced now:
`scripts/collect_metrics.py` wrote [`baseline.json`](baseline.json) before A1,
and the same command re-written after each epic is the before/after:

```bash
python3 scripts/collect_metrics.py --collect-dir .collect \
    --json docs/collect-epics/baseline-after-A.json
diff docs/collect-epics/baseline.json docs/collect-epics/baseline-after-A.json
```

The second instrument is the Gate-1 rejection mix on a real run — the "already
handled" bucket is the target, and it is still a log grep (M4 makes it a
counter):

```bash
grep -c "gate1 accepted=" logs/*.log
```

Baseline at time of writing, script-produced against the artifact on disk
(469 modules, `collector_version` 1): **14.5%** of blocks over budget
(68/469), **2.559** non-derivable rows per block (M2 re-reads it as **3.608**
— M1 could not see the `neighbours` row — and adds **65.1%** redundant chars,
**497/497** blocks with a new row), **678**-char median block,
**0** symbols cut without an announcement, **24** duplicate `config_read` lines
in 11 modules. **0** Gate-1 candidates are suppressed by static facts (the
suppressor has no callers) and **61%** of Pass B claims are dropped by Pass C
(1368 of 2238) — both unchanged, and both still hand-measured until M3/M4.
