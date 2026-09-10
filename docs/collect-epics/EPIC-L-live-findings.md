# EPIC L — tickets that came from the live competition rounds

**Provenance:** not from the trace audit that produced EPIC A/B/C or `PLAN-v2`.
These come from reading five `--auto` runs that were in flight on
2026-09-09 22:10 UTC. The measurements behind them are in
[`LIVE-RUN-VALIDATION.md`](LIVE-RUN-VALIDATION.md).

**Why a separate file:** so the provenance stays honest. v1 (`EPIC-A/B/C`) is
frozen for comparison and v2 (`PLAN-v2.md`) is the recommended plan; neither is
rewritten. These three are additive and none of them depends on either plan.

**Same hard constraint as every other ticket here:**

> `CollectBridge._shrink` is not to be modified, reordered, or replaced.

None of these three touches it. `L1` and `L2` do not touch `tools/collect/` at
all.

| id | what | measured yield | size | risk |
|---|---|---|---|---|
| `L1` | Gate-1 Stage A0 — no LLM call for a location that is not an indexed source file | 74 of 834 gate-1 calls (10%) in one round | S | low |
| `L2` | `probe_config` must distinguish a stale artifact from an absent one | 1 of 5 runs invalidated silently | XS | very low |
| `L3` | Gate-1's presence question vs "add X" goals — **measurement only** | 15% of rejections, 34% on one goal | S | high if implemented blind |
| `L4` | `graph.py` drops `from pkg import mod as alias` edges | `cli.py` shows 5 of 14 imports; `test_map.py` loses its shipped caller | S | low |
| `L5` | `test_map` scans only `tests/`, the graph scans four roots | `main.py`: "13 test files" beside "tests: 5 files" in one block | S | low |
| `L6` | budget loop lets `public_symbols` displace fact rows | at budget 400 `tests` is dropped, `public_symbols` stays | S | low |

`L4`–`L6` were added on 2026-09-10 after rendering the V3 pack against
`../jan-to-fix-pull-v2/.collect/artifact.json`. None of them changes V1–V3:
the rows render exactly what the artifact says. The artifact is what is wrong
(`L4`, `L5`) or the loop under the rows is (`L6`). They go after V5 on the
short path and before V7, because V7 rebuilds the artifact and M2 measures it.

---

## L2 — `probe_config` must say `stale`, not `no_artifact`

**Priority:** High · **Size:** XS · **Files:** `tools/auto/architect.py`,
`tools/auto/collect_bridge.py`

Do this one first. It is the smallest ticket in the whole set and it protects
every measurement that comes after it.

### The defect

`architect.py:975` traces one reason string for two different situations:

```python
if bridge is None or not getattr(bridge, "usable", False):
    logger.warning(
        "architect: probe_enabled=true but no fresh collect artifact is "
        "available ([collect] use_in_auto) — planning without probes this run.",
    )
    self._trace_probe_config(usable=False, reason="no_artifact")
    return None
```

`CollectBridge.usable` is `False` whenever `status != "fresh"`
(`collect_bridge.py:139-142`) — which covers `"stale"` **and** `"absent"`. The
log line says "no fresh collect artifact", which is accurate; the traced
`reason` says `no_artifact`, which is not.

### What it cost, live

`../testtext7` ran the whole plan phase with `usable=False reason=no_artifact`
while `testtext7/.collect/artifact.json` sat on disk at 2.28 MB. It was stale —
artifact `Sep 9 00:11`, newest tracked source `Sep 9 23:13` — and one
`--collect --refresh` would have fixed it. Instead the run produced 459
candidates (the most of the five) with **zero** probe operations, against four
sibling runs that made 763 between them. Nothing in `run.log`, `progress.json`
or the trace says the data was recoverable.

Any A/B comparison between those runs is invalid, and the invalidation is
invisible.

### Do

1. Give `CollectBridge` a public read-only `status` property returning
   `"fresh"` / `"stale"` / `"absent"` — it already reads
   `getattr(self._model, "status", "absent")` inside `usable`; lift that to a
   property and have `usable` call it. No behaviour change.
2. In `_build_probe`, branch the reason:
   ```python
   reason = "stale_artifact" if bridge is not None and bridge.status == "stale" else "no_artifact"
   ```
   and make the `logger.warning` say which, naming
   `--collect --refresh` when it is stale.
3. `reason="bridge_error"` at `architect.py:967` is unchanged.

### Acceptance

- [ ] A stale model traces `reason="stale_artifact"`; an absent one still
      traces `reason="no_artifact"`. Both still trace `usable=False`.
- [ ] The stale warning names `--collect --refresh`; the absent one does not.
- [ ] `CollectBridge.status` on an absent model returns `"absent"` and raises
      nothing. `usable` is unchanged for all three values.
- [ ] `analyze_logs.py` still parses `probe_config` events with the new reason
      (it must not key off the exact string, or it is fixed in the same commit).
- [ ] New: `tests_bugfix/test_probe_config_stale_vs_absent.py`.

### Out of scope

Making a stale artifact usable, refreshing it automatically, or changing
`staleness = warn`. The stance that stale is treated exactly like absent is
deliberate and documented in `collect_bridge.py`'s module docstring — this
ticket changes only what the operator is told.

---

## L1 — Gate-1 Stage A0: a location that is not an indexed source file costs no LLM call

**Priority:** High · **Size:** S · **Files:** `tools/auto/gate1_filter.py`
**Depends on:** nothing. Not gated on `M3` — this is about file type, not about
safety suppression.

### The observation

Gate 1's Stage A is an existence check (`_check_existence`, no LLM); Stage B is
one LLM call per surviving candidate (`_init_presence_provider`,
`architect.py`-independent). In the live round Stage B spent **74 of 834 calls**
— roughly 18 minutes — judging claims against files that are not code:

| location extension | gate-1 LLM calls | confirmed | rejected |
|---|---|---|---|
| `.md` | 28 | 7 | 21 |
| `.ini` | 27 | 10 | 17 |
| `.yaml` | 15 | 5 | 10 |
| `.json` / `.sh` / none | 4 | 1 | 3 |
| **total non-code** | **74** | **23** | **51** |

Typical rejection, verbatim from the trace:

> *"The code shown is a snippet of agents.ini configuration (comments and INI
> section headers), containing no Python code, no test files, and no references
> to test coverage — there is nothing in the excerpt to support the claim."*

The architect proposes these because the cluster it is handed *is* the ini
files. One observed architect prompt was 12 015 chars of `agents.ini` +
`agents_128k.ini` + `agents_256k.ini` + `agents_32k*.ini` verbatim.

### The thing to be careful about

**23 of the 74 were confirmed.** A claim about a `.md` or `.ini` file is not
automatically wrong — a doc that contradicts the code is a real finding, and
`testtext7`'s entire goal is about docstrings and comments. So this ticket must
**not** reject non-code locations outright.

What it may do is skip the *LLM presence check* for a location the collect model
does not index, and route it by task mode instead:

- `task_mode == "docs"`, or the goal text names docs/config → keep today's
  behaviour exactly, LLM call included.
- otherwise → reject at Stage A0 with
  `stage="existence"`, `reason="location is not an indexed source file (<ext>)"`.

### Do

1. In `Gate1Filter.filter`, between Stage A and Stage B, add Stage A0. It runs
   only when a usable collect model is available; with no model it is a no-op
   and every candidate proceeds exactly as today (fail-open, house rule 5).
2. The test is *membership in the collect model*, not an extension allowlist —
   the artifact indexes `.py` and `.java`, and the right question is "does the
   model know this path". Fall back to the extension only when the model is
   absent, in which case the stage is skipped entirely.
3. Record the rejection through the existing `stage="existence"` channel so
   `analyze_logs.py` needs no change and the count shows up in the existing
   Stage-A bucket. Add a distinct `reason` prefix so the two are separable.
4. One config key, defaulting to today's behaviour:
   `[gate1] skip_llm_for_unindexed = true`. Set it `false` and Stage A0 does
   nothing.

### Acceptance

- [ ] A candidate on `agents.ini` with `task_mode="code"` and a fresh model is
      rejected at `stage="existence"` with no LLM call.
- [ ] The same candidate with `task_mode="docs"` reaches Stage B and makes its
      LLM call, exactly as today.
- [ ] With no collect model (absent or stale), every candidate reaches Stage B
      — byte-identical behaviour to today.
- [ ] `skip_llm_for_unindexed = false` disables the stage.
- [ ] A `.java` candidate is **not** rejected — the artifact indexes Java.
- [ ] Counted: the run summary reports how many candidates Stage A0 removed.
- [ ] New: `tests/test_gate1_stage_a0_unindexed.py`.

### How to know it worked

Re-run the same goal against the same tree and compare
`gate1 llm_request` counts in the trace. The target is the 74 from
`LIVE-RUN-VALIDATION.md` §3(a) going to near zero **with the confirmed-candidate
count for `.py` locations unchanged**. If confirmed `.py` candidates drop, the
stage is over-reaching — that is the regression to watch, not the saving.

---

## L3 — Gate 1 asks a presence question that three of five goals cannot answer

**Priority:** Medium · **Size:** S (measurement only) · **Files:** none yet
**This ticket does not change the gate. It produces a number.**

### The observation

Gate 1's prompt, verbatim from the live trace:

> *"Is the claimed problem actually present in the code shown above, and NOT
> already fixed?"*
>
> *"Before answering, find the SPECIFIC line(s) in the code above that the claim
> depends on. If you cannot point to an actual line that supports the claim, the
> claim is not present — reject it…"*

That is a **defect-presence** question and it is a good one; it is most of why
the noise floor came down. But a goal phrased *"find X and **add** Y"* produces
candidates that are proposals, and a proposal is by construction not present in
the code:

| run | goal shape | rejections | rejected as "a proposal, not a defect" |
|---|---|---|---|
| `testtext` | "add a companion test" | 121 | 41 (**34%**) |
| `testtext7` | "report each mismatch" | 103 | 20 (19%) |
| `testtext6` | "add validation or document why safe" | 60 | 6 (10%) |
| `testtext5` | "make each one aware of…" | 44 | 4 (9%) |
| `testtext3` | "add a warning to except blocks" | 177 | 5 (3%) |
| total | | 505 | 76 (15%) |

`testtext3` is the control that makes this readable: its goal also asks to
*add* something, but the **defect** it is grounded in (an `except` that swallows
silently) is visible in the code, so gate 1 can answer the presence question
about it. `testtext`'s goal asks about a test that does not exist — nothing in
the code can show it.

### Why this is not "just fix the prompt"

Loosening gate 1's presence requirement is precisely the change that lets an
88% noise floor back in. The gate is the reason the floor came down. A prompt
that accepts "this would be good to add" accepts almost everything.

There is also a real possibility that the current behaviour is **correct** and
the goals are the problem: a goal that cannot produce a groundable claim may
simply be a badly-shaped goal, and the fix is to phrase goals the way
`testtext3`'s is — name the defect, then the remedy.

### Do (measurement only)

1. Script over `.agent/trace_*.jsonl`: for every rejected candidate, classify
   the goal shape (`defect-grounded` vs `addition-only`) and the rejection
   reason. Output a table like the one above, per run, reproducible.
2. Take the 41 `testtext` rejections in this bucket and hand-adjudicate a
   sample of 15 against the live tree: of those gate 1 rejected as "a proposal",
   how many describe a real gap? That number is the whole decision.
3. Write the finding into this file. **Stop there.**

### The decision this ticket forces

| adjudicated real gaps in the sample | conclusion |
|---|---|
| 0–2 of 15 | gate 1 is right; the goal shape is the defect. Fix the goals in `docs/TASK-jan-selfaudit.md`, change no code. |
| 3–7 of 15 | worth a second gate question for addition-only goals, scoped narrowly and measured against `validate1/truth.csv` before it ships. |
| 8+ of 15 | the gate is discarding a large share of a whole goal class; escalate to its own epic with its own noise-floor measurement. |

### Acceptance

- [ ] The classifier script is committed and its output is reproducible from
      the traces.
- [ ] The 15-item adjudication is recorded with file, symbol and verdict.
- [ ] This file gains the conclusion and the chosen branch.
- [ ] **No change to any gate-1 prompt in this ticket.**


---

## L4 — `graph.py` drops `from <pkg> import <module> as <alias>` edges

**Priority:** High · **Size:** S · **Files:** `tools/collect/graph.py`
**Depends on:** V1 (the queries that expose it). Does not touch `context_assembler.py`.

### The observation

`tools/collect/cli.py:57-65` imports nine sibling modules the same way:

```python
from tools.collect import test_map as test_map_mod
from tools.collect import risk as risk_mod
```

The artifact's `import_edges["tools/collect/cli.py"]` lists five targets, none
of those nine. So `imported_by["tools/collect/test_map.py"]` is three tests plus
`risk.py`, and the V3 row the coder sees for `test_map.py` reads
`callers: 4 modules import this (1 non-test): tools/collect/risk.py` — under a
header that says *do not contradict*. The module's main shipped consumer is
missing from a fact the model is told to trust.

The `from tools.collect import X` form is the house style of `tools/collect/`,
so the rows are wrong precisely for the package the epics are about.

### Do

1. In `resolve_import` (or wherever `from pkg import name` is resolved): when
   `name` resolves to a **module** file under `pkg` — `pkg/name.py` or
   `pkg/name/__init__.py` — emit an edge to that module, not (only) to
   `pkg/__init__.py`. The `as alias` part must be irrelevant to resolution.
2. Keep the existing behaviour when `name` is a symbol inside `pkg/__init__.py`.
3. Deterministic output: sorted, deduplicated, as the tables are today.

### Acceptance

- [ ] A fixture package where `a.py` does `from pkg import b as bb` yields
      `import_edges["pkg/a.py"] ∋ "pkg/b.py"` and `imported_by["pkg/b.py"] ∋ "pkg/a.py"`.
- [ ] `from pkg import SYMBOL` where `SYMBOL` is defined in `pkg/__init__.py`
      still resolves to `pkg/__init__.py` and nothing else.
- [ ] Rebuilt against this repo: `import_edges["tools/collect/cli.py"]` contains
      all nine `*_mod` targets; `callers_of("tools/collect/test_map.py")` contains
      `tools/collect/cli.py`.
- [ ] `scripts/collect_metrics.py` (M1) is re-run and the new
      `import_edges` total is recorded next to the old one in the commit message.
- [ ] New: `tests/test_collect_graph_from_import_module_alias.py`.

---

## L5 — `test_map` scans only `tests/`; the import graph scans four roots

**Priority:** High · **Size:** S · **Files:** `tools/collect/test_map.py`
(possibly the scanner's root list). **Depends on:** V3 (the rows that show it).

### The observation

Every key in the artifact's `test_map` values starts with `tests/`. The graph's
`imported_by` sees `tests/`, `tests_bugfix/`, `.smoke_tests/`,
`.regression_tests/`. The V3 pack for `main.py` therefore says, three lines
apart:

```
callers: entry point — nothing in shipped code imports this; 13 test files do
tests:   5 files: tests/test_auto_g10.py, …, +2
```

Both numbers are true and the block still contradicts itself: 8 of the 13 are
in `tests_bugfix/` and `test_map` never looked there. This is the same
classification V1 shipped in `loader._TEST_PATH_PREFIXES`; `test_map` predates
it and uses its own.

### Do

1. `build_test_map` classifies test modules with the **same** rule V1's loader
   uses (`_TEST_PATH_PREFIXES` + `conftest.py`) — import it or move it to one
   shared place; do not keep two lists.
2. `.smoke_tests/` and `.regression_tests/` mirrors are symlinks into `tests/`;
   a symlinked test counts **once**, under its real path, so the count cannot
   double.
3. `zero_coverage` / `thin_coverage` are recomputed on the wider set; they will
   shrink. Record old and new counts in the commit message.

### Acceptance

- [ ] Fixture with `tests/test_a.py` and `tests_bugfix/test_b.py` both
      importing `m.py`: `test_map["m.py"]` has both.
- [ ] A symlinked mirror of `tests/test_a.py` does not add a second entry.
- [ ] Rebuilt against this repo: the `main.py` pack's `callers` test count and
      its `tests` row count are the same number.
- [ ] M1 re-run; `zero_coverage` before/after in the commit message.
- [ ] New: `tests/test_collect_test_map_all_roots.py`.

---

## L6 — the budget loop lets `public_symbols` displace the fact rows

**Priority:** High · **Size:** S · **Files:** `tools/auto/context_assembler.py`
**Depends on:** V3, V5. **Must not touch `CollectBridge._shrink`** — this is the
loop *above* it, `build_collect_context_block`, and only that.

### The observation

Measured on `tools/auto/coder.py` at `b609492`:

```
budget=1717  callers calls_into tests config_read public_symbols
budget= 400  callers calls_into       public_symbols      ← tests dropped
budget= 250  callers                  public_symbols      ← calls_into dropped
```

The V2 loop skips a row whole when it does not fit and moves on. Only
`public_symbols` knows how to shrink itself, so under pressure it is the row
that survives — and it is the one row the coder can already read from the
target's own source. The plan's rule ("static facts survive, prose goes") and
V5's acceptance both assume the opposite.

A second defect in the same loop: a row's lines are added to `seen` **before**
the budget check that may skip the row, so a row that never rendered still
suppresses identical lines in later rows.

### Do

1. Rows that carry a `+N` tail (`callers`, `calls_into`, `tests`, `neighbours`)
   shrink by dropping names from the end — down to the count alone — before
   the loop gives up on them. The count is the fact; names are the courtesy.
2. `public_symbols` may only take budget that no row above it could use: it is
   rendered last, into whatever is left, never at the cost of a row that would
   have fit in a shorter form.
3. `seen` is updated only for rows that actually render.
4. `_PACK_ROWS` order is unchanged. No new rows.

### Acceptance

- [ ] For `tools/auto/coder.py` against the live artifact, for every budget in
      `(1717, 900, 600, 400, 250)`: the set of rendered rows is a prefix of the
      row order plus `public_symbols` only if it fit *after* the prefix. Rows
      shrink to their count before disappearing.
- [ ] A larger budget never renders fewer rows, nor fewer names in any row,
      than a smaller one (monotonic).
- [ ] A row skipped for budget leaves no trace in `seen`: an identical line in
      a later row still renders.
- [ ] `tests/test_collect_context_block_rows.py` and V5's tests still pass
      unchanged except where they pinned the old displacement.
- [ ] New: `tests/test_collect_block_budget_keeps_fact_rows.py`.
