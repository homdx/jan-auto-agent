# EPIC A — Neighbourhood Fact Pack

**Type:** Epic
**Component:** `tools/collect/` (loader, graph) → `tools/auto/` (context assembly)
**Prepared:** 2026-09-10
**Depends on:** nothing
**Blocks:** EPIC B (B3 wants `FactRow`), EPIC C (C5 wants the docs pack)

---

> **Numbers re-measured — M1** (`scripts/collect_metrics.py`, 2026-09-10).
> The block table below measures the pre-V3 pack on a rebuilt 483-module
> artifact. Against the artifact on disk the same script reports: 469 modules,
> 469 non-empty blocks, 678-char median, 68 blocks over budget (14.5%),
> 2.559 rows per block NOT derivable from the target file, 0 symbols cut
> silently, 24 duplicate `config_read` lines in 11 modules. A1's premise — "the
> block describes the file the coder already has" — is no longer true as
> written: V3's `callers`/`calls_into`/`tests` rows are new facts, 2.559 of
> them per block. Full table: [`baseline.json`](baseline.json).

## Problem

`build_collect_context_block(model, target_file)` renders five kinds of line
and nothing else:

```
line kinds across all 477 non-empty blocks:
  COLLECT (header)  477
  module            477
  public_symbols    477   ← the file the coder already has in full
  config_read       297   ← 24 of them (8%) exact duplicates within one block
  contract            4   ← four in the whole repository
```

The coder's prompt already contains the complete source of every
`target_files` entry (`Coder._build_prompt` → `_read_file_contents`). So the
block spends its entire 1200-char budget restating what is visible three
screens further down, and says nothing about the things that are *not*
visible from inside one file: who calls it, what it calls, which tests cover
it, which of its accesses are already guarded, where it deliberately fails
open, how far a mistake propagates, which config keys it owns, and what its
neighbours are for.

All of that is already computed and already in `artifact.json`. Four tables
are dropped by the loader before any consumer can see them; six more are
loaded and never read.

## Approach

Replace linear concatenation with **priority-ordered assembly of typed rows**,
selected against the budget by one rule:

> the less reachable a fact is from the open source file, the higher it ranks.

`public_symbols` for the target file therefore ranks **last**, which frees the
whole budget for rows that carry new information.

### Target shape

```
COLLECT MODEL (static facts, do not contradict):
module: tools/auto/inner_loop.py
callers      controller.py, outer_loop.py, pipeline.py
calls_into   coder.py, context_broker.py, collect_bridge.py
tests        tests/test_auto_c1.py, tests/test_context_broker_pull_model.py
guarded      14 access(es) on this file already guarded — do not re-report
fails_open   :1368 BLE001 (no rationale recorded)
risk         score 0.81 · blast_radius 7 · zero_coverage no
owns_config  [inner_loop] max_attempts_per_task — also read by controller.py
neighbours   coder.py — builds the grounded prompt and writes files back  (llm)
contract     …
config_read  …
public_symbols …                                      ← lowest priority, cut first
```

Row order in the render is fixed and readable; row order in the *budget* is the
priority ladder. Both are configurable but neither needs to be for the default
to be right.

### Interaction with `_shrink` — do not change it

`CollectBridge._shrink` stays byte-identical. `context_for` keeps its exact
current shape:

```python
raw = <assembled pack>            # A9 changes only what produces `raw`
if len(raw) <= self._max_context_chars:
    return raw
return self._shrink(raw)          # UNCHANGED
```

The pack aims at the budget, so `_shrink` will fire on far fewer blocks — but
it stays on the path and stays as written. A9 adds a characterization test that
pins its behaviour.

---

# Tickets

## A1 — Loader: stop dropping four artifact tables

**Priority:** High · **Size:** S · **Files:** `tools/collect/loader.py`

`_load_from_dir` reads nine of thirteen top-level keys. `import_edges` (483
entries), `imported_by` (483), `entry_points` (396) and `sibling_gaps` are
written by `_artifact_dict` and thrown away on read, so no consumer can ever
reach the import graph.

### Do

1. Add to `CollectModel`:
   ```python
   import_edges: Dict[str, Tuple[str, ...]] = field(default_factory=dict)
   imported_by:  Dict[str, Tuple[str, ...]] = field(default_factory=dict)
   entry_points: Tuple[str, ...] = ()
   sibling_gaps: Tuple[SiblingGap, ...] = ()
   ```
2. Populate them in `_load_from_dir` inside the existing `try:` block, so a
   malformed shape still degrades to `_absent()` exactly as today.
3. Defaults must be empty, not `None` — every existing "absent model behaves
   like collect never ran" test must keep passing untouched.

### Acceptance

- [ ] `load()` on the real artifact returns non-empty `import_edges`,
      `imported_by`, `entry_points`.
- [ ] `load()` on an artifact missing those keys returns empty containers, not
      an absent model (forward/backward schema compatibility both ways).
- [ ] Existing `tests/test_collect_loader*.py` pass unchanged.
- [ ] New: `tests/test_collect_loader_graph_tables.py`.

---

## A2 — `CollectModel` neighbourhood query API

**Priority:** High · **Size:** S · **Files:** `tools/collect/loader.py`
**Depends on:** A1

Add four read-only queries beside the existing `module()` / `contracts_for()` /
`fail_open_for()`. Every one returns an empty list on an absent model.

```python
def callers_of(self, path: str, *, limit: int = 0) -> List[str]:
    """Modules that import `path`, most blast-radius-heavy first.

    Ordered by the caller's own risk_index score (descending), then path, so
    a truncated list keeps the callers that matter. `limit=0` = no limit.
    """

def calls_into(self, path: str, *, limit: int = 0) -> List[str]:
    """Modules `path` imports, first-party only (present in this model)."""

def is_entry_point(self, path: str) -> bool: ...

def risk_for(self, path: str) -> Optional[RiskEntry]: ...
```

Also add `config_owned_by(path) -> List[ConfigMapEntry]` — the `ConfigMapEntry`
rows whose `readers` contain `path`, which is what "which config keys does this
file own, and who else reads them" needs.

### Acceptance

- [ ] `callers_of("tools/llm_stream.py")` returns a non-empty, deterministically
      ordered list on the real artifact.
- [ ] Ordering is stable across two `load()` calls (COLLECT-3 determinism).
- [ ] Absent model → `[]` / `None` / `False` for all five, no exception.
- [ ] New: `tests/test_collect_model_neighbourhood.py`.

---

## A3 — `FactRow` and the priority ladder

**Priority:** High · **Size:** M · **Files:** new `tools/collect/factpack.py`

The core abstraction. No consumer wiring in this ticket — just the model and the
selector, fully unit-testable without a repo.

```python
@dataclass(frozen=True)
class FactRow:
    kind: str          # "callers" | "tests" | "guarded" | … — the render label
    text: str          # the rendered value, no label
    priority: int      # lower = kept longer under budget pressure
    provenance: str    # "static" | "derived" | "llm"  — COLLECT-1 isolation
    weight: int = 1    # rows of the same kind collapse into one line

DEFAULT_PRIORITY = (
    "callers", "calls_into", "tests", "guarded", "fails_open",
    "risk", "owns_config", "neighbours", "contract", "config_read",
    "public_symbols",
)
```

```python
def assemble(rows: Sequence[FactRow], budget: int, header: str) -> str:
    """Render `rows` into a block of at most `budget` characters where possible.

    Greedy by priority: the header and `module:` line are always kept; rows are
    added in priority order while they fit. When a row of kind K is cut, the
    render says so — `"public_symbols: … (+12 more, cut for budget)"` — the same
    honesty `_format_module_block`'s "… and N more symbol(s) not listed" already
    provides and `build_collect_context_block`'s silent `[:20]` does not.

    Never raises. Returning a block LONGER than `budget` is legal and expected
    when a single row exceeds it on its own; the caller (CollectBridge) then
    hands it to the existing `_shrink`, which is not this module's business.
    """
```

### Do

- Rows of the same `kind` merge into one rendered line, comma-joined, in the
  order given.
- Deduplicate identical `(kind, text)` pairs before rendering — this alone
  removes the 24 duplicate `config_read` lines measured on the real artifact.
- Keep `provenance` on the row but do **not** render it per-row; A8 renders it
  only for `llm` rows, which are the only ones where it changes meaning.

### Acceptance

- [ ] `assemble([], 1200, hdr)` → `""` (nothing to say → no block, matching
      today's contract).
- [ ] A row set that fits comes back whole, in priority order.
- [ ] A row set that overflows drops lowest-priority rows first and annotates
      every partial kind with a count.
- [ ] One row longer than the whole budget comes back intact and over budget —
      `_shrink`'s job, not `assemble`'s.
- [ ] Duplicate `(kind, text)` rows render once.
- [ ] New: `tests/test_collect_factpack.py` — pure unit, no filesystem.

---

## A4 — Port today's five line kinds onto `FactRow`

**Priority:** High · **Size:** M · **Files:** `tools/auto/context_assembler.py`
**Depends on:** A3

Behaviour-preserving refactor, done before any new row type is added, so a
regression here is unambiguous.

### Do

1. `build_collect_context_block` builds `FactRow`s for `contract`,
   `config_read`, `public_symbols` and calls `assemble`.
2. `_COLLECT_HEADER` and the `module:` / `parse_error:` lines stay exactly as
   they are and are passed as the `header` argument.
3. `public_symbols` gets priority **last** and its cap moves from a silent
   `[:20]` to `assemble`'s budget logic with an announced remainder.
4. The "only header + bare module line → return `''`" early-out is preserved.

### Acceptance

- [ ] For every module in the real artifact where the old block was ≤1200 chars,
      the new block contains the same facts (order may differ; content may not).
- [ ] The 28 modules with >20 symbols no longer lose 296 symbols silently —
      either they fit, or the remainder is announced.
- [ ] Duplicate `config_read` lines are gone (24 on the real artifact).
- [ ] Existing `tests/test_collect_context_block*.py` updated, not deleted, and
      the assertions that pinned the old *content* still hold.
- [ ] New: `tests_bugfix/test_collect_block_symbol_cap_announced.py`.

---

## A5 — `callers` / `calls_into` / `tests` rows

**Priority:** High · **Size:** S · **Files:** `tools/auto/context_assembler.py`
**Depends on:** A2, A4

The first rows that carry genuinely new information.

### Do

```python
callers = model.callers_of(target_file, limit=cfg.pack_max_callers)   # default 5
callees = model.calls_into(target_file, limit=cfg.pack_max_callees)   # default 5
tests   = model.test_map.get(target_file, ())[:cfg.pack_max_tests]    # default 3
```

- Render paths relative, comma-joined, never absolute.
- `tests` already exists on `CollectModel` and is currently read only by
  Gate 1's `tests_covering`; this is its second consumer, no loader change.
- An entry-point module (`model.is_entry_point`) with no callers renders
  `callers  (entry point — nothing imports this)` rather than an empty row: a
  module nothing imports is a real, useful fact, not a miss.

### Acceptance

- [ ] `tools/auto/inner_loop.py` block names its real importers.
- [ ] `main.py` renders the entry-point form.
- [ ] A module absent from the model still returns `""` overall.
- [ ] New: `tests/test_collect_pack_neighbourhood_rows.py`.

---

## A6 — `guarded` / `fails_open` rows

**Priority:** High · **Size:** M · **Files:** `tools/auto/context_assembler.py`
**Depends on:** A4

The 2814 `guarded_accesses` and 682 `except_sites` records (0.5 MB rebuilt on
every collect) currently have exactly one potential reader — `is_safe()` — and
it has no callers. This is the row that changes auto-mode output quality most
directly.

### Do

1. `guarded` row — count and, when they fit, line numbers:
   ```
   guarded      14 access(es) already guarded (:212, :318, :1102, +11) — do not re-report
   ```
   Source: `record.guarded_accesses` filtered to `status == GUARDED`.
2. `fails_open` row — `model.fail_open_for(target_file)`, with the
   **undocumented** ones (`rationale is None`) listed first, since those are the
   ones a reviewer actually wants:
   ```
   fails_open   :1368 BLE001 (no rationale), :904 OSError (documented: partial artifact is a build failure)
   ```
3. Cap each row's enumerated detail; the *count* is never dropped, only the
   enumeration.

### Why the wording matters

"do not re-report" is not decoration. The largest recurring false-positive class
in this project's auto-generated improvement lists is "add error handling here"
against code that already has it. The row exists to put the contradicting fact
in front of the model before it writes the claim.

### Acceptance

- [ ] `tools/auto/inner_loop.py` renders a non-zero `guarded` count.
- [ ] A module with no guarded accesses renders no row at all (not "0").
- [ ] Undocumented fail-opens sort before documented ones.
- [ ] Both rows are `provenance="static"`.
- [ ] New: `tests/test_collect_pack_safety_rows.py`.

---

## A7 — `risk` / `owns_config` rows

**Priority:** Medium · **Size:** S · **Files:** `tools/auto/context_assembler.py`
**Depends on:** A2, A4

`risk_index` (483 entries) and `config_map` (186) are loaded today and read by
nobody.

### Do

1. `risk` row from `model.risk_for(path)`:
   ```
   risk         score 0.81 · blast_radius 7 · loc 1998 · zero_coverage no
   ```
   Suppress the row entirely when `score` is 0 — a zero-risk module gains
   nothing from a row saying so.
2. `owns_config` row from `model.config_owned_by(path)` — the key plus the
   *other* readers, which is the sibling-drift signal:
   ```
   owns_config  [inner_loop] max_attempts_per_task — also read by controller.py, outer_loop.py
   ```
   Cap at `pack_max_config_keys` (default 4); `has_mode_override` keys sort
   first (they are the ones with per-mode variants that get missed).

### Acceptance

- [ ] A high-risk module renders the row; a score-0 module renders none.
- [ ] `owns_config` names co-readers, not just the key.
- [ ] New: `tests/test_collect_pack_risk_config_rows.py`.

---

## A8 — `neighbours` row: the Pass B payoff

**Priority:** High · **Size:** M · **Files:** `tools/auto/context_assembler.py`
**Depends on:** A2, A5

483 LLM calls per full build produce `ModuleRecord.summary`, read today only by
`render.py` and `verifier.py`. This ticket gives it its first agent consumer —
and does it in the one place where prose is not redundant.

### Design decision, stated explicitly

The row carries the `purpose` of the target file's **neighbours**, never of the
target file itself. The target's source is in the prompt; a paraphrase of it is
noise. Its callers' and callees' purposes are not in the prompt and cannot be
derived from it.

### Do

```
neighbours   coder.py — builds the grounded prompt, writes files back  (llm)
             context_broker.py — resolves missing-context requests from source  (llm)
```

1. Take the top `pack_max_neighbours` (default 3) entries from
   `callers_of` + `calls_into`, in that order.
2. Use `summary.purpose` truncated at the first sentence or
   `pack_neighbour_purpose_chars` (default 110), whichever is shorter.
3. Skip a neighbour whose purpose is empty — 228 modules on the real artifact
   have one, 219 of them tests (see EPIC C, C7, which recovers most of these).
4. **Render `(llm)` on every line of this row.** It is the only `provenance="llm"`
   row in the pack; COLLECT-1's isolation is preserved by labelling, not by
   exclusion, and the label is what tells the model this line is weaker evidence
   than the ones above it.
5. Priority sits below `risk`/`owns_config`: under budget pressure the static
   facts survive and the prose is what goes.

### Acceptance

- [ ] The row renders `(llm)` and nothing else in the pack does.
- [ ] A neighbour with an empty purpose is skipped, not rendered blank.
- [ ] With `[collect] llm_summaries = false` (no summaries in the artifact) the
      row is absent and the rest of the pack is unaffected.
- [ ] Under a budget that fits only half the pack, `neighbours` is dropped
      before `guarded`.
- [ ] New: `tests/test_collect_pack_neighbour_purpose.py`.

---

## A9 — Wire the pack into `CollectBridge` — without touching `_shrink`

**Priority:** High · **Size:** M · **Files:** `tools/auto/collect_bridge.py`
**Depends on:** A4–A8

### Do

1. `context_for` keeps its current three-line shape. The only change is that
   `raw` now comes from the pack. **Do not** modify `_shrink`, its overshoot
   tolerance, its logging, its truncation notice, or `shrink_calls`.
2. Add a per-run memo: `self._pack_memo: Dict[str, str]`, keyed by
   `(target_file, budget)`. `context_for` is called once per task, and the same
   file is a target in many tasks; today each one re-assembles and, when over
   budget, re-pays an LLM shrink call. The bridge is already build-once-per-run
   (`make_collect_bridge`'s contract), so the memo is safe and bounded by the
   number of distinct target files.
3. `context_for_many` keeps budgeting each file independently — changing that is
   a separate decision and not this epic's.

### Characterization test — the guard on `_shrink`

Add `tests/test_collect_bridge_shrink_contract.py` pinning today's behaviour so
no later ticket can drift it:

- summarizer returns text within `budget * 1.15` → that text is returned verbatim;
- summarizer returns text beyond `budget * 1.15` → hard truncation with the
  `[+N chars truncated by CollectBridge]` notice, and the overshoot is logged;
- summarizer raises → hard truncation, failure logged;
- `summarizer_call is None` → hard truncation, `shrink_calls` stays 0;
- `shrink_calls` increments once per shrink attempt, including failed ones.

### Acceptance

- [ ] `git diff` for this ticket touches no line inside `_shrink`.
- [ ] The characterization test passes before and after the ticket.
- [ ] Second `context_for("x.py")` in one run makes zero additional LLM calls.
- [ ] On the real artifact the number of blocks over budget drops from 50/477;
      record the new figure in the commit message.

---

## A10 — Persist the call graph

**Priority:** Medium · **Size:** M · **Files:** `tools/collect/cli.py`,
`tools/collect/loader.py`, `tools/auto/context_assembler.py`
**Depends on:** A5

`graph.build_call_edges(root, modules)` builds a real call graph —
`module -> frozenset(modules whose unambiguous public symbols it calls)` — and
**has no caller anywhere in the codebase**. It is not in `CollectContext`, not
in `_artifact_dict`, not on disk.

Import edges answer "what does this file import". Call edges answer "what does
this file actually use", which is a much better `calls_into` row.

### Do

1. Add `call_edges` to `CollectContext`, populate it in `build_context`, persist
   it in `_artifact_dict`, load it in `_load_from_dir` (same guarded block).
2. `calls_into` prefers `call_edges` when present and falls back to
   `import_edges` when absent — old artifacts keep working.
3. `called_by` becomes available for free (reverse of the same graph); render it
   in the `callers` row as `callers (calls)` vs `callers (imports)` so the two
   are never conflated.
4. Cost check: `build_call_edges` re-reads and re-parses every module. Measure
   it. If it adds more than ~30% to Pass A wall time (Pass A + C is ~16 s for
   483 modules today), put it behind `[collect] call_graph = true`, default
   **on**, and say so in the commit message.

### Acceptance

- [ ] `artifact.json` gains a `call_edges` key; `--refresh` on an unchanged tree
      still makes zero LLM calls.
- [ ] An artifact without `call_edges` loads and the pack falls back cleanly.
- [ ] Wall-time delta for a full build recorded in the commit message.
- [ ] New: `tests/test_collect_call_graph_persisted.py`.

---

## A11 — Config surface for the pack

**Priority:** Medium · **Size:** S · **Files:** `agents*.ini`, `README.md`
**Depends on:** A5–A8

Every knob added by this epic, in `[collect]`, each read through the same
guarded `try/except ValueError → documented default` pattern the section already
uses everywhere:

```ini
# COLLECT-USE-A: the per-task fact pack. Rows are selected by priority until
# max_context_chars_auto is spent; public_symbols ranks last because the coder
# already has the target file's full source in the same prompt.
pack_enabled                 = true
pack_priority                = callers, calls_into, tests, guarded, fails_open, risk, owns_config, neighbours, contract, config_read, public_symbols
pack_max_callers             = 5
pack_max_callees             = 5
pack_max_tests               = 3
pack_max_config_keys         = 4
pack_max_neighbours          = 3
pack_neighbour_purpose_chars = 110
```

- `pack_enabled = false` restores the exact pre-epic block (A4's ported render
  with new rows suppressed) — the escape hatch for a regression in the field.
- An unknown name in `pack_priority` is logged and skipped, never fatal —
  matching the `[gates]` section's existing convention.
- Absent section/keys → the defaults above, so no existing `agents*.ini` needs
  editing to keep working.

### Acceptance

- [ ] Every key malformed → documented default + one warning, no crash
      (`pack_max_callers = abc`, `pack_enabled = maybe`).
- [ ] `pack_enabled = false` produces the A4 block exactly.
- [ ] Keys documented in `README.md`'s `[collect]` block.
- [ ] New: `tests_bugfix/test_collect_pack_config_guards.py`.

---

## A12 — Measure the result

**Priority:** Medium · **Size:** S · **Files:** `scripts/` (new helper)
**Depends on:** A9

The epic's own acceptance evidence. A small script that loads the artifact,
assembles every module's pack and reports:

```
modules                       483
non-empty packs               477
median pack                   ___ chars      (baseline 544)
packs over budget             ___ / 477      (baseline 50)
rows carrying new information ___ / pack     (baseline 0)
bytes of artifact reachable   ___ %          (baseline 38%)
```

Add the same numbers to `docs/COLLECT-24-SUMMARY.md` (or a new
`docs/COLLECT-USE-SUMMARY.md`) so the next round can diff against them.

### Acceptance

- [ ] Script runs read-only against any repo with a `.collect/`.
- [ ] Before/after numbers recorded in the epic's final commit message.
