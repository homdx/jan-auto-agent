# COLLECT-USE — plan v2 (simplified)

**Prepared:** 2026-09-10 · **Branch:** `competition`
**Replaces nothing.** v1 (`EPIC-A/B/C`) stays on disk next to this file so the
two can be compared. Same goal, same evidence, fewer moving parts.
**Measurement lives in [`EPIC-M-metrics.md`](EPIC-M-metrics.md)** and applies to
both plans.

---

## 1. What this actually improves: collection, or use?

Both, but not equally, and the split is worth stating plainly because v1 blurred
it.

### Collection improves in **cost and completeness**, not in correctness

The facts being collected are already right. Pass A is deterministic, Pass C
grounds every LLM claim, provenance is isolated by construction. Nothing here
makes a *better* fact. What changes:

| | today | after |
|---|---|---|
| `--collect`, one file changed | **483 LLM calls** | 1 (V7) |
| `--collect --no-llm` | wipes all 483 summaries | preserves them (V8) |
| modules with a usable Pass B purpose | **241 / 469** | ~460 (V11) |
| symbol signatures | `name(...)` for 4030 / 4030 | real parameter lists (V10) |

That is a cheaper, more complete artifact. It is not the point of this work.

### Use is where the actual change is — and here is the mechanism

Today the per-task block contains **zero rows** a reader of the target file
could not derive from the target file, because `Coder._build_prompt` already
puts that file's complete source in the same prompt. Measured: ~100% of block
characters are redundant; `contracts` fires on 4 modules out of 469, and that
is the entire non-redundant content of the whole system.

Six concrete ways that changes:

1. **Blast radius before an edit.** `tools/auto/coder.py` is imported by 25
   modules. The coder editing it today knows about none of them. After V3 the
   first line of the block says so. That is the difference between "rename the
   parameter" and "rename the parameter and notice it is a public entry point".
2. **Which tests to expect.** 11 test files cover `coder.py`. The Gate-2 loop
   currently discovers that by failing. After V3 it is in the prompt, and the
   metric that should move is **Gate-2 attempts per task** (M5).
3. **The dominant false positive, contradicted in advance.** `coder.py` has 7
   sites that swallow exceptions on purpose. The single largest class of
   auto-generated proposals in this repo is "add error handling here" against
   code that already decided not to. V4 puts `fails_open :1028, :1143, … — these
   swallow on purpose` in front of the model *before* it writes the claim.
   Suppressing a bad finding after it is written (v1's EPIC B) is the expensive
   way to fix the same problem; supplying the contradicting fact is the cheap
   one.
4. **Fewer round trips.** `ArchProbe` exists because the model has to ask for
   neighbourhood facts. Those requests, hits and misses are already counted by
   `analyze_logs.py`. If the pack carries what the probe was asking for, probe
   requests and misses drop — a Tier-2 win that needs no ground truth to
   verify, and the check that catches a pack that merely adds noise.
5. **Pass B finally has an agent consumer.** 483 LLM calls per full build
   produce prose read only by a 725 KB Markdown file no agent opens. V5 spends
   it where prose is not redundant: the *neighbours'* purpose, never the target's.
6. **What is supplied stops being wrong.** From the second task of every `--auto`
   run onward, the block describes the tree as it was before the run started,
   under a header reading *"static facts, do not contradict"*. V9 fixes that.
   It adds no rows at all and is still one of the highest-value tickets here.

**The honest summary:** collection gets cheaper; use goes from ~0 non-derivable
facts per prompt to 4–8, and from silently-stale to correct. `EPIC-M` exists so
that sentence becomes a number before and after.

---

## 2. Review of v1: what measurement changed

v1 was written from a reachability trace. Re-measuring the tables against the
restored artifact before writing v2 contradicted four of its design decisions.

| v1 claim | measured | consequence |
|---|---|---|
| "483 modules" throughout | artifact has **469** | every hand-counted number in v1 is suspect → `M1` |
| A1: recover `sibling_gaps` | **0 entries** | dropped from v2 |
| A6: `guarded` row is "the row that changes output quality most directly" | **23** GUARDED locations repo-wide, on **15 of 469** modules; 2620 locations are UNGUARDED | **row cut** |
| A7: `owns_config` names the keys a file owns | `config_map.readers` for a common key lists 11+ modules — it encodes *sharing*, not ownership | folded into the existing `config_read` row as a co-reader count |
| A7 example: `risk score 0.81` | score is `3138`, an unbounded composite; the useful fields are `unguarded_count`, `undocumented_fail_open_count`, `zero_coverage` | render the components, not the score |
| EPIC B: 8 tickets on static suppression | total surface that can ever answer `safe=True`: **~144 locations** (23 guard + 117 fail-open + 4 contract) out of 2646 | **B is gated on `M3` and shrinks to ~40 lines if it survives** |
| B1: symbol-only citations resolve via the symbol index | the adjudicated corpus is **35/54 method-level** (`Class.member`); the index has **0 methods** | the hardest half of B1 cannot work on the real corpus; cut |
| A5: `callers` from `imported_by` | 8 of coder.py's first 8 importers are **test files** | callers row filters tests out — they are already the `tests` row |

Three structural over-builds, independent of the data:

* **A3's `FactRow` + `assemble()` + configurable `pack_priority`.** A frozen
  dataclass with 5 fields, a generic greedy selector, a new module, and an
  ini-configurable priority string — to render at most ten lines whose order
  nobody will ever want to change. v2 uses an ordered tuple of small functions
  in the module that already renders the block.
* **A11's seven config keys.** Every one is a knob on a default that is correct.
  v2 ships one boolean.
* **A10's call-graph persistence.** `import_edges` already answers `calls_into`
  well enough to render the row. Re-parsing 469 modules to sharpen it is a
  separate, optional piece of work — moved to the appendix.

Nothing in v1's *analysis* is retracted. What changes is how much machinery each
finding justifies.

---

## 3. The v2 design, in one page

### One ordered tuple, in the file that already renders the block

No new module. `tools/auto/context_assembler.py` gains:

```python
# Highest value first. "Value" = how hard this fact is to get from the target
# file's own source, which the coder already has in full in the same prompt.
_PACK_ROWS = (
    ("callers",        _row_callers),        # who breaks if I change this
    ("calls_into",     _row_calls_into),
    ("tests",          _row_tests),          # incl. "no test covers this file"
    ("fails_open",     _row_fails_open),     # do not "add error handling" here
    ("risk",           _row_risk),
    ("neighbours",     _row_neighbours),     # Pass B purpose, labelled (llm)
    ("contract",       _row_contract),       # today's row, unchanged
    ("config_read",    _row_config_read),    # today's row + co-reader count
    ("public_symbols", _row_public_symbols), # today's row — now LAST
)

def build_collect_context_block(model, target_file, *, task_mode="code", budget=None):
    head = [_COLLECT_HEADER, f"module: {target_file}"]      # unchanged
    used, body = sum(len(x) + 1 for x in head), []
    for name, render in _PACK_ROWS:
        line = render(model, target_file)                   # None -> row absent
        if not line:
            continue
        if budget and used + len(line) + 1 > budget:
            continue                                        # try the next, shorter row
        body.append(line)
        used += len(line) + 1
    return "\n".join(head + body) if body else ""           # early-out unchanged
```

Each `_row_*` is 3–10 lines and independently testable. Priority *is* the tuple
order. Cutting a row under budget pressure *is* the `continue`. That is the
whole of v1's A3 + A4 + A11, and it fits on this page.

### `_shrink` is untouched — the same guarantee as v1

```
build_collect_context_block(..., budget=max_context_chars_auto)   # aims at budget
        |
len(raw) <= max_context_chars ?
        |  no                              |  yes
CollectBridge._shrink(raw)              return raw
   UNCHANGED, byte for byte
```

`context_for` keeps its exact three-line shape; only what produces `raw`
changes. V6 adds a characterization test pinning `_shrink`'s current behaviour
(overshoot tolerance, truncation notice, `shrink_calls`, logging) so no later
ticket drifts it. Both V6 and M4 carry the acceptance line *"`git diff` touches
no line inside `_shrink`"*.

### What a real block looks like — `tools/auto/coder.py`, real data

Today (544 chars median across the repo; this one is a qualname list plus config
reads, all of it visible in the source three screens down):

```
COLLECT MODEL (static facts, do not contradict):
module: tools/auto/coder.py
public_symbols: Coder, Coder.generate, CoderResult, _read_file_contents, … (12)
config_read: [api] active; [coder] max_context_chars; …
```

After V1–V5, from the same artifact, no new collection:

```
COLLECT MODEL (static facts, do not contradict):
module: tools/auto/coder.py
callers      25 modules import this (2 non-test): inner_loop.py,
             tools/skills/loader.py
calls_into   llm_stream.py, context_assembler.py, search_agent.py,
             block_extractor.py, config_safe.py, +2
tests        11 files: tests/test_auto_c2.py, tests/test_coder_prompt_domain.py,
             tests/test_coder_smart_context.py, +8
fails_open   7 sites swallow exceptions on purpose: :1028 ValueError|TypeError,
             :1143 OSError, :2237 Exception, +4 — do not add error handling here
risk         loc 2507 · imported by 25 · unguarded accesses 45 · covered yes
neighbours   inner_loop.py — per-round attempt loop (Gate 2), up to
             max_attempts_per_task rounds  (llm)
             context_broker.py — resolves missing_context by pulling source  (llm)
config_read  [api] active (+10 other readers); [coder] max_context_chars
public_symbols Coder, Coder.generate, CoderResult, … (12)
```

Every line above the `contract` row is a fact the model cannot obtain by reading
the file it was given. That is the entire thesis, and M2 is the number for it.

---

## 4. Tickets

House rules are unchanged from `INDEX.md`: verify against live source before
implementing, one local commit per ticket, never push, new behaviour → `tests/`
and fixes → `tests_bugfix/`, and run the two real roots one after the other
(`.smoke_tests/` and `.regression_tests/` are symlink views onto `tests/`):

```bash
python3 -m pytest tests -q --timeout=180 && python3 -m pytest tests_bugfix -q --timeout=180
```

Every new consumer is fail-open: an absent, malformed or stale artifact degrades
to "no collect data" and never raises into a run.

---

### Stage 0 — measure (blocking)

`M1`, `M2`, `M3` from [`EPIC-M-metrics.md`](EPIC-M-metrics.md). Nothing below
starts until `baseline.json` is committed. `M3` decides whether V12 and V13 get
built at all.

---

### Stage 1 — supply: the pack

#### V1 — Loader keeps the import graph, and three queries on top

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

#### V2 — The block becomes an ordered row list

**Priority:** High · **Size:** M · **Files:** `tools/auto/context_assembler.py`
**Depends on:** nothing *(v1: A3 + A4, without the new module)*

Behaviour-preserving. No new row types in this ticket, so a regression here is
unambiguous.

**Do**

1. Introduce `_PACK_ROWS` as in §3, populated with today's three renderers
   (`contract`, `config_read`, `public_symbols`) moved into `_row_*` functions
   verbatim.
2. `_COLLECT_HEADER` and the `module:` / `parse_error:` lines are unchanged and
   stay outside the loop.
3. `public_symbols` moves to last. Its silent `[:20]` becomes budget-driven with
   an announced remainder — `… (+12 more, cut for budget)` — matching the
   honesty `_format_module_block` already has.
4. Deduplicate identical rendered rows (24 duplicate `config_read` lines exist).
5. `budget=None` renders everything, so every existing caller and test keeps
   working before V6 wires the budget in.
6. The "header + bare module line only → return `''`" early-out is preserved.

**Acceptance**

- [ ] For every module whose old block was within budget, the new block carries
      the same facts (order may differ; content may not).
- [ ] The 28 modules with >20 symbols no longer drop 296 symbols silently.
- [ ] Duplicate `config_read` lines gone.
- [ ] `tests/test_collect_context_block*.py` updated, not deleted; the
      assertions that pinned content still hold.
- [ ] New: `tests_bugfix/test_collect_block_symbol_cap_announced.py`.

---

#### V3 — Rows: `callers`, `calls_into`, `tests`

**Priority:** High · **Size:** S · **Files:** `tools/auto/context_assembler.py`
**Depends on:** V1, V2 *(v1: A5)*

The first rows that carry new information.

**Do**

1. `callers` renders the **count first**, then up to 5 names:
   `25 modules import this (7 non-test): controller.py, inner_loop.py, +5`.
   The count is the fact; the names are the courtesy.
2. `calls_into` — up to 5 first-party imports.
3. `tests` — from `test_map` (301 entries, 78 modules covered), up to 3 names
   plus a count. When the module is in `zero_coverage` (223 of 469), render
   `no test covers this file` instead. **The absence is the more useful fact**
   and costs nothing.
4. An entry-point module with no callers renders
   `callers (entry point — nothing imports this)`, not an empty row.
5. Paths always relative.

**Acceptance**

- [ ] `tools/auto/coder.py` names its 7 non-test importers and says 25.
- [ ] A `zero_coverage` module renders the no-tests form.
- [ ] `main.py` renders the entry-point form.
- [ ] A module absent from the model still yields `""` overall.
- [ ] New: `tests/test_collect_pack_neighbourhood_rows.py`.

---

#### V4 — Rows: `fails_open`, `risk`

**Priority:** High · **Size:** S · **Files:** `tools/auto/context_assembler.py`
**Depends on:** V1, V2 *(v1: A6 + A7, with the `guarded` row cut)*

**Do**

1. `fails_open` from `fail_open_registry` (117 sites over 64 modules): the count,
   then up to 4 `:line type` pairs, then the sentence that is the point of the
   row — `— these swallow exceptions on purpose, do not add error handling here`.
   A site *with* a rationale renders it; only 22 of 117 have one, so the row
   must read correctly without.
2. `risk` from `risk_index`, rendering the **components**, never the raw score
   (3138 for `coder.py` — unbounded and meaningless in a prompt):
   `loc 2507 · imported by 25 · unguarded accesses 45 · covered yes`.
   Suppress the row when nothing in it is notable.
3. **No `guarded` row.** Measured: 23 GUARDED locations across 469 modules, on
   15 modules. A row that fires on 3% of files and says "0" on the rest is
   budget spent on nothing. `guarded_accesses` keeps its one real consumer
   (`is_safe`, Stage 3).
4. `owns_config` is **not** a new row — `config_map.readers` encodes sharing,
   not ownership. Instead, today's `config_read` row gains a co-reader count:
   `[api] active (+10 other readers)`. That is the sibling-drift signal, in one
   parenthesis, with no new row and no new table.

**Acceptance**

- [ ] `coder.py` renders 7 fail-open sites and the warning sentence.
- [ ] A module with no fail-open sites renders no row (not "0").
- [ ] The `risk` row never prints the composite score.
- [ ] Both rows are `static` provenance; no LLM text reaches them.
- [ ] New: `tests/test_collect_pack_safety_rows.py`.

---

#### V5 — Row: `neighbours` — the Pass B payoff

**Priority:** High · **Size:** M · **Files:** `tools/auto/context_assembler.py`
**Depends on:** V1, V3 *(v1: A8, unchanged in substance)*

483 LLM calls per full build produce `ModuleRecord.summary`. Its only readers
today are `render.py` (a 725 KB Markdown file for humans) and `verifier.py`
(which checks it). This is its first agent consumer.

**The design decision, stated so it is not quietly reversed:** the row carries
the purpose of the target's **neighbours**, never of the target itself. The
target's source is in the prompt; a paraphrase of it is noise. Its callers' and
callees' purposes are not in the prompt and cannot be derived from it.

**Do**

1. Up to 3 entries from `callers_of` + `calls_into`, in that order.
2. `summary.purpose`, cut at the first sentence or 110 chars, whichever is
   shorter.
3. Skip a neighbour with an empty purpose (228 modules today; V11 recovers ~219
   of them).
4. **Render `(llm)` on every line.** It is the only non-static row in the pack.
   COLLECT-1's provenance isolation is preserved by labelling, not by exclusion,
   and the label is what tells the model this line is weaker evidence than the
   ones above it.
5. It sits below `risk` in `_PACK_ROWS`: under budget pressure the static facts
   survive and the prose is what goes.

> **Short-path note (2026-09-10).** V4 is not on the short path, so `risk` and
> `fails_open` do not exist when this ticket runs. Read "below `risk`" as
> **directly after `tests`** — last of the fact rows, before the V2 trio — and
> the budget acceptance as "`neighbours` is dropped before `tests`". Do not
> implement V4 rows to satisfy the wording; that is a second ticket's work.

**Acceptance**

- [ ] The row renders `(llm)`; nothing else in the pack does.
- [ ] An empty purpose is skipped, not rendered blank.
- [ ] With no summaries in the artifact the row is absent and the rest of the
      pack is unaffected.
- [ ] Under a budget fitting half the pack, `neighbours` is dropped before
      `tests` (before `fails_open` once V4 lands).
- [ ] New: `tests/test_collect_pack_neighbour_purpose.py`.

---

#### V6 — Wire the budget and the memo into `CollectBridge`

**Priority:** High · **Size:** S · **Files:** `tools/auto/collect_bridge.py`,
`agents*.ini` *(v1: A9 + A11, one boolean instead of seven keys)*

**Do**

1. Pass `budget=self._max_context_chars` into `build_collect_context_block`.
   `context_for` keeps its exact three-line shape; **do not** touch `_shrink`,
   its overshoot tolerance, its logging, its truncation notice or `shrink_calls`.
2. Add a per-run memo keyed by `(target_file, budget)`. The same file is a target
   in many tasks; today each one re-assembles and, when over budget, re-pays an
   LLM shrink call. The bridge is already build-once-per-run, so the memo is
   safe and bounded by the number of distinct target files.
3. One config key, in `[collect]`:
   ```ini
   # COLLECT-USE: per-task fact pack. Rows are emitted highest-value-first until
   # max_context_chars_auto is spent; public_symbols ranks last because the coder
   # already has the target file's full source in the same prompt.
   pack_enabled = true
   ```
   `false` restores the V2 block (today's three rows). Caps (5 callers, 5
   callees, 3 tests, 4 fail-open sites, 3 neighbours, 110 purpose chars) are
   module constants with a comment, not ini keys — no one is going to tune them,
   and each one would be a config-guard test.
4. `context_for_many` keeps budgeting each file independently.

**Characterization test — the guard on `_shrink`**

`tests/test_collect_bridge_shrink_contract.py`, pinning today's behaviour:
summarizer within `budget * 1.15` → returned verbatim; beyond it → hard
truncation with the `[+N chars truncated by CollectBridge]` notice, overshoot
logged; summarizer raises → hard truncation, failure logged;
`summarizer_call is None` → hard truncation, `shrink_calls` stays 0;
`shrink_calls` increments once per attempt including failed ones.

**Acceptance**

- [ ] `git diff` touches no line inside `_shrink`.
- [ ] The characterization test passes before and after this ticket.
- [ ] A second `context_for("x.py")` in one run makes zero additional LLM calls.
- [ ] `pack_enabled = false` reproduces the V2 block exactly.
- [ ] A malformed value warns once and uses the default.
- [ ] Blocks over budget: record the new figure against the M1 baseline (50/477).

---

### Stage 2 — producer: cost and honesty

Independent of Stage 1. Each is a small, self-contained win.

#### V7 — `--collect` on a stale tree goes incremental; fix the docs

**Priority:** High · **Size:** M · **Files:** `tools/collect/cli.py`, `main.py`,
`README.md`, `Collect.MD` *(v1: C1 + C2, one commit)*

482 wasted LLM calls per changed file, on the default path, documented
backwards.

| command | tree | LLM calls |
|---|---|---|
| `--collect` | 1 file changed | **483** |
| `--collect --refresh` | 1 file changed | **1** |

```
main.py:855    "--refresh: unconditional full rebuild, ignoring freshness."   WRONG
README.md:67   "--refresh forces a full rebuild"                              WRONG
Collect.MD:163 "--refresh (diff-driven incremental rebuild)"                  right
```

**Do**

1. `action_collect` on a stale tree delegates to `action_refresh`. On a fresh
   tree it stays the no-op it is.
2. Add `--rebuild` (and `/collect --rebuild`) for the unconditional full
   rebuild — today's `_full_build` path.
3. `--refresh` keeps working and keeps meaning what it means.
4. The result message says which path ran: `collect: incrementally refreshed 1
   changed module(s)` vs `rebuild: 469 module(s) re-summarized`.
5. Rewrite every help string and doc line to match, including `main.py`'s
   interactive `/collect` block and `README.md:486`'s worked example.

**Migration note:** a `collector_version` mismatch must still force a full
rebuild — `manifest.is_fresh` already handles it. This ticket changes the
*stale-tree* path only, not the *schema-mismatch* path.

**Acceptance**

- [ ] One file changed + `--collect` → 1 LLM call.
- [ ] `--collect --rebuild` → one call per module.
- [ ] Fresh tree + `--collect` → still a pure no-op, zero writes.
- [ ] `grep -rn "full rebuild"` over `main.py`, `README.md`, `Collect.MD`: every
      hit describes real behaviour.
- [ ] New: `tests_bugfix/test_collect_stale_is_incremental.py`.

---

#### V8 — `--no-llm` preserves existing summaries

**Priority:** High · **Size:** M · **Files:** `tools/collect/cli.py`, `main.py`
*(v1: C3)*

Documented as "a purely structural build". It is a full rebuild that writes
`summary: null` for every module — 469 summaries destroyed, silently.

**Do**

1. `--no-llm` merges: modules keep whatever `summary` the previous artifact had,
   the same way `action_refresh` carries unchanged records forward.
2. Add `--drop-summaries` for today's destructive behaviour — a legitimate thing
   to want, just not silently.
3. A carried-forward summary whose module hash changed gets
   `provenance = "llm-stale"`. Add the value to the provenance enum and to
   `field_provenance()`; do not invent it locally.
4. `verification_report.json` is currently not written by a `--no-llm` build
   (11 files, not 12). With summaries preserved there *are* claims: either verify
   and write it, or write a report saying Pass C did not run. A missing file is
   the one option to avoid.

**Acceptance**

- [ ] `--collect --no-llm` after a normal build → summaries survive.
- [ ] `--collect --no-llm --drop-summaries` → 0 summaries, and says so.
- [ ] A changed module's carried-forward summary is marked stale.
- [ ] `.collect/` file count stable across build modes, or the difference is
      explained in the result message.
- [ ] New: `tests_bugfix/test_collect_no_llm_preserves_summaries.py`.

---

#### V9 — Freshness is checked once and then believed all run

**Priority:** High · **Size:** M · **Files:** `tools/auto/collect_bridge.py`,
`tools/auto/controller.py` *(v1: C8)*

`collect_bridge.py`'s own docstring promises that a stale model is treated
exactly like an absent one. `status` is computed once inside `load()` and the
bridge is then cached for the lifetime of the `Controller`. `--auto` edits and
commits source files. Reproduced against live code:

```
at run start:                      usable=True  status=fresh
after a task edits the tree:       usable=True  status=fresh
does the block know the new symbol?  False
header still reads:                "COLLECT MODEL (static facts, do not contradict):"
```

After Stage 1 the pack asserts *more* about a file than today's block does, so
the cost of believing it grows with every ticket above. This one adds no rows
and is still among the highest-value tickets in the plan.

**Do**

1. **Invalidate on write, not on a timer.** The controller already knows which
   files each task touched — it commits them. After a successful commit:
   ```python
   bridge.invalidate(paths)     # NEW
   ```
   `context_for` / `pull_symbol` / `module_symbols` return `""` for a dirty path.
   Clean paths are untouched: one edited file must not blind the pack for the
   other 468.
2. **Then, optionally, repair.** `[collect] auto_refresh_between_tasks`
   (default `false`): instead of blinding a dirty path, run the existing
   `action_module` for it — one module, one LLM call — and fold the fresh record
   back into the in-memory model.
3. Keep the build-once contract for the model *object*: patch records in place,
   never call `load()` twice. `test_collect_model_loaded_once_per_run` must keep
   passing unchanged — if it cannot, the design is wrong, not the test.
4. Emit `collect_miss(reason="dirty")` (M4) so a run reports how many blocks this
   suppressed.

**Acceptance**

- [ ] The reproduction above inverts: after an edit, `context_for` on that path
      returns `""` (or, with the flag on, post-edit facts).
- [ ] An unedited path keeps its block for the whole run.
- [ ] `tools.collect.loader.load` still called exactly once per run.
- [ ] With the flag on, a 5-task run where tasks 1 and 3 touch the same file
      gives task 3 the post-task-1 facts.
- [ ] New: `tests_bugfix/test_collect_bridge_stale_after_task_commit.py`.

---

#### V10 — Real signatures

**Priority:** Medium · **Size:** M · **Files:** `tools/collect/ast_facts.py`,
`tools/collect/model.py` *(v1: C4)*

`PROBE_INSTRUCTIONS` tells the Architect that `facts <symbol>` "returns its
signature". It returns `name(...)` for **4030 of 4030** symbols. The elision is
deliberate and documented, but for an agent that has to *call* the function the
parameter list is the whole point.

**Do**

1. `signature = f"{node.name}({ast.unparse(node.args)})"` for functions; classes
   keep `name(...)` unless a bare `__init__` is trivially available.
2. Truncate at 160 chars with a trailing `, …)`.
3. `ast.unparse` needs 3.9+; fall back to `name(...)` on `AttributeError` rather
   than raising.
4. This changes artifact content for every module → bump `collector_version` so
   `manifest.is_fresh` forces one rebuild. Say so in the commit message; it is
   the one intentional full rebuild in this plan, and it is why V10 lands after
   V7 makes rebuilds explicit.

**Note on methods.** `extract_symbols` walks `ast.iter_child_nodes` — top level
only — so there are **0 methods** among the 4030 symbols, and
`_qualname_matches`'s dotted-suffix branch can never match. `PROBE_INSTRUCTIONS`
already says methods cannot be looked up, so this is consistent, not broken.
Indexing methods is a separate decision with real cost and is out of scope —
but see `M3`: the adjudicated finding corpus is 35/54 method-level, so if
Stage 3 ever needs symbol-level resolution, this is the ticket it depends on.

**Acceptance**

- [ ] `pull_symbol("build_chat_request")` returns a real parameter list.
- [ ] A 40-symbol module's `module_symbols` output stays readable.
- [ ] Two builds produce byte-identical signatures.
- [ ] New: `tests/test_collect_real_signatures.py`.

---

### Stage 3 — bug hunting (V11 unconditional; V12–V13 gated on M3)

#### V11 — Pass C stops eating test-file summaries

**Priority:** High · **Size:** M · **Files:** `tools/collect/verifier.py`
**Depends on:** nothing *(v1: B7)*

Pass C drops a claim whose cited symbol belongs to another module. For a test
file that rule is inverted from reality: a test's entire purpose is to name
symbols from the module under test.

```
claims extracted             2238
dropped                      1368  (61%)
modules left with no purpose  228
  of which test files         219   ← this ticket
  of which source files         9
```

This is also what makes V5 worth its budget: every recovered purpose is a
neighbour row that can render.

**Do**

1. In `verify_repo`, when the module under verification is a test file
   (`tests/`, `tests_bugfix/`, `.smoke_tests/`, `.regression_tests/`, or
   `test_*.py` / `*_test.py` anywhere), extend the citable-symbol set from
   "symbols defined here" to "symbols defined here **plus every module this test
   imports**".
2. `import_edges` already knows what a test imports — no new analysis.
3. A test citing a symbol from a module it does **not** import is still a
   hallucination and still drops.
4. Make it a parameter of the verification call, not a filename check buried in
   the claim extractor, so the rule is visible and testable.

**Acceptance**

- [ ] A full rebuild leaves ≤20 modules with an empty purpose (from 228); record
      the exact number.
- [ ] A test citing an unimported symbol still drops.
- [ ] Non-test rules unchanged — the 9 source-file drops stay dropped.
- [ ] New: `tests_bugfix/test_collect_verifier_test_file_citations.py`.

---

#### V12 — Gate-1 Stage A2, minimal — **only if M3 says the ceiling is > 0**

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

#### V13 — One safety note for the candidates V12 does *not* suppress

**Priority:** Low · **Size:** S · **Files:** `tools/auto/gate1_grounding.py`
**Depends on:** V12 *(v1: B3, two functions merged into one)*

Gate 1 already builds `collect_contract_note` and `existing_test_coverage_note`
for the Stage-B prompt. Add one more, for the ambiguous candidates V12
deliberately leaves alone:

```python
def already_handled_note(collect_bridge, cited) -> Optional[str]:
    """'Static analysis records this site as already guarded by <guard>.' or
    'This site fails open deliberately: <exception> — <rationale>.'

    The fail-open half renders only when a rationale exists — 22 of 117 sites
    have one, and an undocumented fail-open is not evidence of intent and must
    not be presented as if it were."""
```

**Acceptance**

- [ ] Undocumented fail-open produces no note.
- [ ] The note appears in the Stage-B prompt and is absent when the model is
      absent or stale.
- [ ] New: `tests/test_gate1_grounding_safety_notes.py`.

---

### Stage 4 — finish

#### V14 — Turn on docs mode

**Priority:** Medium · **Size:** S · **Files:** `agents*.ini`,
`tools/auto/context_assembler.py` **Depends on:** V2–V5 *(v1: C6)*

`make_collect_bridge` already selects the flag by `task_mode`; only the switch
is off. The one consumer where Pass B's prose is exactly the right material is
the one consumer that is disabled.

**Do**

1. A second tuple, `_PACK_ROWS_DOC`, selected by `task_mode`:
   ```python
   ("neighbours", "purpose", "entry_point", "calls_into", "callers", "tests",
    "config_read", "public_symbols")
   ```
   Note `purpose`: in docs mode the **target's own** purpose is wanted, unlike
   code mode, because a docs task often writes *about* a file whose full source
   is not in the prompt. `entry_points` (382 entries, no reader today) becomes a
   row — documentation cares which modules are roots.
2. `use_in_doc = true` in `agents_128k.ini`.
3. Check the `existence` gate's rejection rate on a docs run does not rise; if
   the writer starts citing neighbour paths as the subject, prefix the row
   `(context, not the subject of this document)`.

**Acceptance**

- [ ] A docs-mode task gets a pack; a code-mode pack is unchanged.
- [ ] The `existence` gate's docs-run rejection rate does not increase.
- [ ] New: `tests/test_collect_pack_docs_mode.py`.

---

#### V15 — Docs sync and the consumer column

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

**Correction (2026-09-12, all eleven landed):** V6's *budget wire* is not
droppable. `CollectBridge.context_for` still calls
`build_collect_context_block` without `budget=`, so L6's row-shrinking loop
never runs in a live `--auto`; every overshoot goes to `_shrink`. V6 step 1 is
the twelfth ticket of the short path. Status of every ticket:
`STATUS.md`.

| | v1 | v2 |
|---|---|---|
| tickets | 29 | 15 + 6 measurement |
| new modules | 2 (`factpack.py`, `gate1_safety.py`) | 0 |
| new config keys | 9 | 2 (`pack_enabled`, `auto_refresh_between_tasks`) |
| new artifact keys | 1 (`call_edges`) | 0 |
| rows in the pack | 11 | 9 |
| `_shrink` | untouched | untouched |
| baseline before the work | none | `baseline.json`, committed |
