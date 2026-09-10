# EPIC C — Producer cost, honesty, observability

**Type:** Epic
**Component:** `main.py`, `tools/collect/cli.py`, `tools/collect/ast_facts.py`,
`tools/auto/run_trace.py`, `README.md`, `Collect.MD`
**Prepared:** 2026-09-10
**Depends on:** A1–A3 for C5 only; every other ticket is independent

---

> **Numbers re-measured — M1** (`scripts/collect_metrics.py`, 2026-09-10).
> The producer-cost table below was written against `../jan-to-fix-pull-v2` at
> 483 modules. The artifact on disk has 469 modules and 4030 symbols, every one
> elided to `name(…)` (`signatures_elided 4030 / 4030`) and 0 methods indexed.
> So C1's stale-tree full rebuild is 469 LLM calls, not 483, and C4's
> signature work covers 4030 symbols, not 4093. Both conclusions are
> unchanged — C1's diff-driven path and C4's real signatures are still the
> whole point. Full table: [`baseline.json`](baseline.json).

## Problem

Three separate classes of defect on the producer side, all measured against
`../jan-to-fix-pull-v2` (483 modules) with a local stub LLM:

**1. The default command is the expensive one, and the help says the opposite.**

| command | tree state | LLM calls | wall |
|---|---|---|---|
| `--collect --check` | 1 file changed | 0 | 5.1 s |
| `--collect` | fresh | 0 | — |
| `--collect` | **1 file changed** | **483** | 25.5 s* |
| `--collect --refresh` | 1 file changed | **1** | — |
| `--collect --refresh` | unchanged | 0 | 19.5 s |
| `--collect --no-llm` | 1 file changed | 0 | 16.4 s |

\* against an instantly-answering stub; on a real provider the 483 calls are the
entire wall time.

`action_collect` on a stale tree calls `_full_build`, which re-summarizes every
module. `action_refresh` is the diff-driven one. The help text has it backwards:

```
main.py:855    "With --collect: unconditional full rebuild, ignoring freshness."   WRONG
README.md:67   "--refresh forces a full rebuild"                                   WRONG
Collect.MD:163 "--refresh (diff-driven incremental rebuild)"                       right
action_refresh.__doc__ "diff-driven incremental rebuild (COLLECT-24)"              right
```

**2. `--no-llm` destroys Pass B silently.** Documented as "a purely structural
build". It is a full rebuild that writes `summary: null` for every module:
483 summaries → 0. `--refresh` preserves unchanged modules' summaries;
`--collect --no-llm` does not.

**3. Nothing is observable.** `tools/auto/run_trace.py` has no collect events.
Whether the block reached the prompt, whether it was shrunk, whether a probe
resolved — none of it appears in a trace. ArchProbe has its own telemetry; the
coder-side block has none.

---

# Tickets

## C1 — `--collect` on a stale tree goes diff-driven

**Priority:** High · **Size:** M · **Files:** `tools/collect/cli.py`, `main.py`

482 wasted LLM calls per changed file is the largest single cost in the
pipeline, and it is the default path.

### Do

1. `action_collect` on a stale tree delegates to `action_refresh` instead of
   `_full_build`. On a fresh tree it stays the no-op it is today.
2. Add `--rebuild` (and `/collect --rebuild`) for the unconditional full
   rebuild, wired to a new `action_rebuild` that is today's `_full_build` path.
   `parse_collect_args` gains it alongside `--check` / `--refresh` / `--module`.
3. `--refresh` keeps working and keeps meaning what it means — nobody's muscle
   memory breaks.
4. The result message must say which path ran, so a log answers "why did this
   take twenty minutes": `collect collect: incrementally refreshed 1 changed
   module(s)` vs `collect rebuild: full rebuild, 483 module(s) summarized`.

### Migration note

An artifact built by an older `collector_version` still forces a full rebuild —
`manifest.is_fresh` already handles that and must keep doing so. This ticket
changes the *stale-tree* path only, not the *schema-mismatch* path.

### Acceptance

- [ ] One file changed + `--collect` → 1 LLM call (from 483).
- [ ] `--collect --rebuild` → 483 calls.
- [ ] `--collect` on a fresh tree → still a pure no-op, zero writes.
- [ ] `collector_version` mismatch → still a full rebuild.
- [ ] New: `tests_bugfix/test_collect_stale_is_incremental.py`.

---

## C2 — Fix the flag documentation

**Priority:** High · **Size:** S · **Files:** `main.py`, `README.md`
**Depends on:** C1

### Do

Rewrite the four help strings and the two doc lines to describe the post-C1
behaviour:

```
--collect    Build the structural project model into [collect] dir. Fresh tree:
             no-op. Stale tree: diff-driven incremental rebuild — only changed
             modules are re-summarized.
--check      Only report freshness. Writes nothing, anywhere.
--refresh    Force the diff-driven incremental rebuild even when the tree looks
             fresh (recomputes derived artifacts).
--rebuild    Unconditional full rebuild: every module re-summarized. Expensive —
             one LLM call per module.
--module P   Re-scan only P and patch it into the existing artifact.
--no-llm     Skip Pass B. Existing summaries are preserved (see --drop-summaries).
```

Same wording in `main.py`'s interactive `/collect` help block, `README.md:67`
and `README.md:486`'s worked example.

### Acceptance

- [ ] No help string, README line, or `Collect.MD` line describes a behaviour
      the code does not have. Grep for "full rebuild" and check every hit.

---

## C3 — `--no-llm` preserves existing summaries

**Priority:** High · **Size:** M · **Files:** `tools/collect/cli.py`, `main.py`

### Do

1. `--no-llm` merges: modules keep whatever `summary` the previous artifact had,
   exactly the way `action_refresh` carries unchanged modules' records forward.
   A module whose source changed keeps its now-stale summary but is marked —
   see (3).
2. Add `--drop-summaries` for the current destructive behaviour, since
   "rebuild the structure and throw the prose away" is a legitimate thing to
   want, just not silently.
3. When a merged summary belongs to a module whose hash changed, set
   `LLMSummary.provenance` to `"llm-stale"` so `render.py` can mark it and Pass C
   can decide about it. **Do not** invent a new provenance value without adding
   it to the provenance enum and to `field_provenance()`.
4. `verification_report.json` is currently not written by a `--no-llm` build
   (11 files, not 12). With summaries preserved there *are* claims to verify —
   either verify them and write the report, or write a report that says
   explicitly that Pass C did not run. A missing file is the one option to
   avoid.

### Acceptance

- [ ] `--collect --no-llm` after a normal build → 483 summaries survive (from 0).
- [ ] `--collect --no-llm --drop-summaries` → 0 summaries, and says so.
- [ ] A changed module's carried-forward summary is marked stale.
- [ ] `.collect/` file count is stable across build modes, or the difference is
      explained in the result message.
- [ ] New: `tests_bugfix/test_collect_no_llm_preserves_summaries.py`.

---

## C4 — Real signatures

**Priority:** Medium · **Size:** M · **Files:** `tools/collect/ast_facts.py`,
`tools/collect/model.py`

`PROBE_INSTRUCTIONS` tells the Architect that `facts <symbol>` "Returns its
signature and contracts". What it returns is `name(...)` — a placeholder, for
**4093 of 4093 symbols**. The elision is deliberate and documented
(`extract_symbols`: "COLLECT-4's job is symbol inventory, not full signature
reconstruction"), but for an agent that has to *call* the function, the
parameter list is the whole point.

### Do

1. `signature = f"{node.name}({ast.unparse(node.args)})"` for functions;
   for classes keep `name(...)` unless a bare `__init__` is trivially available.
2. Truncate at `[collect] max_signature_chars` (default 160) with a trailing
   `, …)` — some signatures in this tree are long.
3. `ast.unparse` needs Python 3.9+. Check `requirements.txt` / CI; if 3.8 must
   be supported, fall back to `name(...)` on `AttributeError` rather than
   raising.
4. This changes `artifact.json` content for every module → bump
   `collector_version` so `manifest.is_fresh` forces one rebuild. Say so in the
   commit message; it is the one intentional full rebuild in this epic.

### Acceptance

- [ ] `pull_symbol("build_chat_request")` returns a real parameter list.
- [ ] `module_symbols` output stays readable — check a 40-symbol module for line
      length after the change.
- [ ] Determinism holds: two builds produce byte-identical signatures.
- [ ] New: `tests/test_collect_real_signatures.py`.

### Note on methods

`extract_symbols` walks `ast.iter_child_nodes` — top level only — so there are
**0 methods** among the 4093 symbols, and `_qualname_matches`'s dotted-suffix
branch (`symbol.endswith("." + name)`) can never match anything this producer
writes. `PROBE_INSTRUCTIONS` already tells the model methods cannot be looked
up, so this is consistent, not broken. Indexing methods is a separate decision
with real cost (4093 → maybe 12000 symbols) and is **out of scope here** — but
if it is ever taken, that dead branch is what starts working.

---

## C5 — Collect events in the run trace

**Priority:** Medium · **Size:** M · **Files:** `tools/auto/run_trace.py`,
`tools/auto/collect_bridge.py`, `tools/auto/inner_loop.py`
**Depends on:** A9

Today a run's trace cannot answer "did collect contribute anything to this
task". `analyze_logs.py` can report probe sequencing to the percent; the coder
block has nothing.

### Do

Four events, all cheap, all structured like the existing ones:

| event | fields |
|---|---|
| `collect_block` | `task_id`, `target_file`, `chars`, `rows_kept`, `rows_cut` |
| `collect_shrink` | `task_id`, `target_file`, `before`, `after`, `path` = `llm` \| `truncate` |
| `collect_miss` | `task_id`, `target_file`, `reason` = `absent` \| `stale` \| `unknown_module` |
| `collect_summary` | per run: blocks injected, chars injected, shrink calls, misses |

`collect_shrink` observes `_shrink` from the outside — from `context_for`, by
comparing lengths and reading `shrink_calls`. **It does not modify `_shrink`.**

`analyze_logs.py` learns all four and prints the per-run summary next to the
existing probe line.

### Acceptance

- [ ] A `--dry-run` against a stub emits the events and the run summary.
- [ ] `git diff` touches no line inside `_shrink`.
- [ ] Zero events when no bridge is wired in — an off feature stays silent.
- [ ] New: `tests/test_run_trace_collect_events.py`.

---

## C6 — Turn on docs mode

**Priority:** Medium · **Size:** M · **Files:** `agents*.ini`,
`tools/auto/context_assembler.py`
**Depends on:** A4–A8

`use_in_doc = false`. `make_collect_bridge` already selects the key by
`task_mode`, so the plumbing exists and only the switch is off — which means the
one consumer where Pass B's prose is exactly the right material is the one
consumer that is disabled.

### Do

1. A docs-mode priority ladder, distinct from the code one:
   ```
   neighbours, purpose, entry_point, calls_into, callers, tests, owns_config, public_symbols
   ```
   Note `purpose` — in docs mode the **target file's own** `purpose`/`notes` are
   wanted, unlike code mode (A8), because a docs task is often writing *about* a
   file whose full source is not necessarily in the prompt.
2. `is_entry_point` becomes a first-class row: documentation cares which modules
   are roots. `entry_points` has 396 entries and no reader.
3. Set `use_in_doc = true` in `agents_128k.ini`.
4. The `existence` gate is the registry default in docs mode and is
   deterministic — check the new rows do not tempt the writer into citing a
   neighbour path as if it were the subject. If they do, prefix them
   `(context, not the subject of this document)`.

### Acceptance

- [ ] A docs-mode task gets a pack; a code-mode task's pack is unchanged.
- [ ] The `existence` gate's rejection rate on a docs run does not increase.
- [ ] New: `tests/test_collect_pack_docs_mode.py`.

---

## C7 — Prune what stays unreachable

**Priority:** Low · **Size:** S · **Files:** `tools/collect/cli.py`, `README.md`
**Depends on:** A5–A8, B2, C6

After EPICs A and B land, re-run the reachability trace from the audit and
settle every remaining table honestly. For each of `gates`, `sibling_gaps`,
`thin_coverage`, `zero_coverage` and anything else still at zero readers, pick
one and write it down:

- **used** — a consumer landed; note where.
- **human-only** — it feeds a rendered `.md` and that is its whole job; say so
  in `Collect.MD` next to the table.
- **retire** — nothing reads it and nothing plausibly will; remove the producer
  and the artifact key, and bump `collector_version`.

The nine rendered pages (~950 KB: `MODULE_MAP.md` alone is 725 KB) are
legitimately human-only and should be labelled as such rather than treated as an
unfinished integration.

### Acceptance

- [ ] Every top-level `artifact.json` key has a documented consumer or a
      documented "human-only" / "retired" status.
- [ ] `Collect.MD`'s table gains a "consumer" column.

---

## C8 — Freshness is checked once and then believed for the whole run

**Priority:** High · **Size:** M · **Files:** `tools/auto/collect_bridge.py`,
`tools/auto/controller.py`

### The defect

`collect_bridge.py`'s own module docstring states the contract:

> the model is only ever consulted when `status == "fresh"`. `"stale"` is
> treated exactly like `"absent"` here.

`status` is computed **once**, inside `load()`, at the moment
`_get_collect_bridge` first runs — and the bridge is then cached for the
lifetime of the `Controller` (`_collect_bridge_cache`, pinned by
`test_collect_model_loaded_once_per_run`). Nothing re-checks it afterwards.

But `--auto` *edits and commits source files*. From the first committed task
onward the artifact no longer matches the tree, while the in-memory model still
reports `fresh` and `usable` still returns `True`. Every later task is served
pre-edit facts under a header that says they are ground truth.

Reproduced against the live code and the real artifact:

```
at run start:                     usable=True  status=fresh
after a task edits the tree:      usable=True  status=fresh
does the block know the new symbol?  False
block still labelled:             "COLLECT MODEL (static facts, do not contradict):"
```

This is worse than the coverage drop it looks like from the outside. A task that
edits `tools/foo.py` and a later task that targets `tools/foo.py` again get a
symbol list, a `guarded` count (A6) and a `risk` score (A7) describing the file
as it was before the run started — and after EPIC A the pack asserts *more*
about the file than it does today, so the cost of believing it grows.

### Do

1. **Invalidate on write, not on a timer.** The controller already knows which
   files each task touched (it commits them). After a successful commit, mark
   those paths dirty on the bridge:
   ```python
   bridge.invalidate(paths)        # NEW
   ```
   `context_for` / `pull_symbol` / `module_symbols` return `""` for a dirty path
   — the documented "stale is treated exactly like absent" behaviour, now
   actually applied. Clean paths are unaffected: one edited file must not blind
   the pack for the other 482.
2. **Then, optionally, repair.** `[collect] auto_refresh_between_tasks`
   (default `false`): instead of blinding a dirty path, run `action_module` for
   it — the existing incremental patch path, one module, one LLM call — and
   fold the fresh `ModuleRecord` back into the in-memory model.
3. Keep the build-once contract for the model *object*. Patch records in place;
   never call `load()` a second time. `test_collect_model_loaded_once_per_run`
   must keep passing unchanged — if it cannot, the design is wrong, not the test.
4. Emit `collect_miss(reason="dirty")` (C5) so a run reports how many blocks
   this suppressed.

### Acceptance

- [ ] The reproduction above inverts: after an edit, `context_for` on the edited
      path returns `""` (or, with the flag on, the post-edit facts).
- [ ] An unedited path keeps its block for the whole run.
- [ ] `tools.collect.loader.load` is still called exactly once per run.
- [ ] With `auto_refresh_between_tasks = true`, a 5-task run where tasks 1 and 3
      touch the same file gives task 3 the post-task-1 facts.
- [ ] New: `tests_bugfix/test_collect_bridge_stale_after_task_commit.py`.

---

## C9 — Docs sync

**Priority:** Low · **Size:** S · **Files:** `README.md`, `Collect.MD`,
`docs/COLLECT-24-SUMMARY.md`
**Depends on:** all of A, B, C

Last ticket in the epic. Bring every document that describes collect into line
with what shipped:

- `README.md` `[collect]` section: the new `pack_*` keys, `--rebuild`,
  `--drop-summaries`, the corrected `--refresh` description, `use_in_bughunt`
  and `use_in_doc` now doing something.
- `Collect.MD`: the COLLECT-19/21/22/24 rows gain their real consumer; the
  consumer column from C7.
- A new `docs/COLLECT-USE-SUMMARY.md` in the shape of `COLLECT-24-SUMMARY.md`,
  carrying the before/after table from A12 and the measured tables from B.
- The `[collect]` comment block in every `agents*.ini` that still says
  `use_in_*` flags are "a no-op today".

### Acceptance

- [ ] `grep -rn "no-op today" agents*.ini` returns nothing about `use_in_*`.
- [ ] Someone reading only `README.md` can turn the pack on, tune it, and know
      what it costs.
