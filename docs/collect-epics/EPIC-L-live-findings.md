# EPIC L — three tickets that came from the live competition round

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
