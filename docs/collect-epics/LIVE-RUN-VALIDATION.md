# Live-run validation — do the collected facts help the competition runs?

**Snapshot:** 2026-09-09 22:10 UTC, taken while five `--auto` runs were mid-flight.
**Subject:** the five processes running against `../testtext`, `../testtext3`,
`../testtext5`, `../testtext6`, `../testtext7`, all with
`--config agents_128k.ini --dry-run`.
**Method:** read-only. Nothing was started, stopped or written to. Every number
below comes from the runs' own `.agent/trace_*.jsonl` and `.agent/run.log`.

**Reproduce it:** `scripts/trace_round_snapshot.py` reads the same traces and
writes the same counters as JSON. The runs were in flight, so its output moves;
the snapshot taken at 22:33 UTC is committed as
`baseline-live-2026-09-09.json`, and the tables in this file are the 22:10 one.
Both are the same measurement twenty minutes apart — where they differ, the
counts are simply further along.

```bash
python3 scripts/trace_round_snapshot.py ../testtext ../testtext3 ../testtext5 \
    ../testtext6 ../testtext7 --out docs/collect-epics/baseline-live-2026-09-09.json
```

This file exists because the epics were written from a *reconstructed* trace of
a single `--auto` run. Five real runs were in flight, so the premise could be
checked against them instead of against a harness. It was — and one claim in
the epics turns out to be understated, one live defect showed up that neither
plan had, and one run is producing competition data while blind.

---

## The short answer

> **Yes, but not where the epics said the first win was.** The collect block
> reaches only the coder, and all five runs pass `--dry-run`, which skips the
> execute phase outright (`dry-run: plan phase complete; execution skipped`).
> The coder is unreachable **by construction**, not by timing. 1629 LLM calls
> have been made and **not one of them carried a collect block.**

---

## 1. What the five runs are actually doing

| run | goal (abbrev.) | probe usable | candidates | architect calls | gate-1 done | confirm rate | est. gate-1 left |
|---|---|---|---|---|---|---|---|
| `testtext` | add companion tests for hollow guards | yes | 451 | 98 | 236 (52%) | 48% | ~50 min |
| `testtext3` | log what `except` swallows | yes | 326 | 215 | 258 (79%) | 28% | ~15 min |
| `testtext5` | regex boundary markers inside fences | yes | 389 | 181 | 58 (15%) | 14% | ~101 min |
| `testtext6` | JSON writes bypassing schema validation | yes | 410 | 208 | 110 (27%) | 45% | ~85 min |
| `testtext7` | cross-module doc guarantees | **no** | 459 | 93 | 172 (37%) | 39% | ~76 min |
| **total** | | | **2035** | **795** | **834 (41%)** | **37%** | **1201 candidates** |

All five report `code_done: 0, code_total: 0` in `progress.json`. Every run is
still in plan/gate-1. Wall clock so far: ~2 h 05 m per run.

Gate 1 costs **12.9–18.4 s per candidate**, one LLM call each, serial.
2035 candidates × ~15 s ≈ **8.5 hours of gate-1 alone** across the round, and
1201 of those candidates have not been judged yet.

---

## 2. The finding that matters: facts reach nothing that is running

`grep -c "COLLECT MODEL (static facts" trace_*.jsonl` over all five traces:

```
testtext   0     (323 llm_requests)
testtext3  0     (458 llm_requests)
testtext5  0     (225 llm_requests)
testtext6  0     (303 llm_requests)
testtext7  0     (248 llm_requests)
```

`[collect] use_in_auto = true` is set in the config these runs are using. The
artifact is on disk in all five. The block is simply never built, because
`build_collect_context_block` is called from one place — `Controller
.collect_context_for(target_file)` → `Coder._build_prompt` — and **the coder
phase has not started.**

So the two phases that consume 100% of the round's wall clock:

| phase | LLM calls so far | collect facts it receives |
|---|---|---|
| architect | 795 | the probe, on request only — and one run has none |
| gate 1 | 834 | one hand-wired line (below) |
| coder | 0 | the full block |

This is exactly the premise of both plans, but stronger than either stated it.
`INDEX.md` says *"the per-task block describes the file the coder can already
read"*. The live runs say something sharper: **in a `--dry-run` round the block
does not exist at all.**

That is worth being precise about, because it is not a timing accident. The
execute phase is skipped by the flag:

```
[2026-09-09T22:26:09Z] dry-run: plan phase complete; execution skipped
[2026-09-09T22:26:09Z] [AUTO-F2] phase execute skipped (dry-run)
```

Competition prep is planning-only, so for the way these runs are actually used,
the coder-side pack delivers exactly nothing — gate 1 and the architect are the
only surfaces that exist. EPIC A's rows start paying at the moment the coder
runs, and in this mode the coder never does.

### The one exception, and it is the proof the mechanism works

Every gate-1 prompt carries this, and it is the only collect-sourced fact in
any of the 1629 calls:

```
Existing test coverage on record (from `collect`) for `main.py`:
`tests/test_auto_g10.py`, `tests/test_auto_h1.py`,
`tests/test_collect_main_llm_wiring.py`, `tests/test_main_resume_checkpoint.py`,
`tests/test_story_3_2.py`. If the claim is about MISSING tests, check whether
one of these files already covers the specific behavior described before
confirming — module-level coverage existing doesn't by itself prove the exact
claim is covered, but it's a strong hint to look closely.
```

That is `CollectBridge.tests_covering`, wired straight into the gate-1 prompt,
one line, with a usage instruction attached. It is precisely the `tests` row of
the v2 pack (`V3`), already shipped into the one place it was needed most.

**It is the template.** The pack is not a new idea in this codebase — it is this
line, generalised to the other eight rows and moved upstream to where the
calls actually happen.

---

## 3. Where gate-1's 519 rejections come from

Every rejection carries a one-sentence `reason`. Bucketed over 519 rejected
candidates (regex over the reason text, so treat the edges as approximate):

| bucket | share | can a fact row preempt it? |
|---|---|---|
| "cannot verify from the code shown" / "no evidence in the excerpt" | 63% | **partly** — see below |
| "already handled / intentional / by design" | 8% | **yes** — this is the `fails_open` row |
| "proposal, not a present defect" | 15% | **no** — gate-contract mismatch, `L3` |
| other | ~14% | mixed |

Three concrete sub-cases were checked against the live tree rather than
inferred:

**(a) The candidate points at a file collect does not index — 74 of 834 (10%).**

| location extension | gate-1 calls | confirmed | rejected |
|---|---|---|---|
| `.py` | 680 | 261 | 417 |
| `.md` | 28 | 7 | 21 |
| `.ini` | 27 | 10 | 17 |
| `.yaml` | 15 | 5 | 10 |
| `.java` | 3 | 0 | 3 |
| `.json` / `.sh` / none | 4 | 1 | 3 |

The architect proposes these because the cluster handed to it *is* the ini
files — one architect prompt observed was 12 015 chars of `agents.ini`,
`agents_128k.ini`, `agents_256k.ini`, `agents_32k*.ini` verbatim, most of it
comments about KV-cache RAM. Gate 1 then spends ~15 s per candidate learning
that an INI file contains no Python. **74 calls ≈ 18 minutes of this round.**
No LLM is needed to know a `.md` file is not an indexed module — this is
ticket **`L1`**.

**(b) The cited line window does not contain the code the claim is about.**

Rare but real: 12 of 834 windows contain zero substantive code lines, and 10 of
those 12 were rejected. One is worth quoting because it is a **true positive
being thrown away**, on the run whose entire goal was to find it:

> `testtext3`, goal: *"Find except blocks that swallow an exception without
> logging"*. Candidate: `main.py, lines 17–20` — the `try: import readline /
> except ImportError: pass` block. Gate 1 was shown lines 17–20, which are a
> comment banner, and correctly answered *"the code shown contains no [loop]
> section… no grounding evidence supports it."*
>
> The code is at **lines 13–16**. Off by three. Verified in the live tree.

This is not a fact-supply problem and no pack row fixes it. It is a
citation-window problem, and it is small (12/834). Recorded here so it is not
re-discovered as a mystery later.

**(c) Gate 1 asks a question three of the five goals cannot answer.**

Gate 1's prompt is *"Is the claimed problem actually present in the code shown
above, and NOT already fixed?"* — a **defect-presence** question. A goal phrased
"find X and **add** Y" produces candidates that are proposals, and the honest
answer to a presence question about a proposal is "no".

| run | rejections | rejected as "a proposal, not a defect" |
|---|---|---|
| `testtext` ("add a companion test") | 121 | 41 (**34%**) |
| `testtext7` ("report each mismatch") | 103 | 20 (19%) |
| `testtext6` | 60 | 6 (10%) |
| `testtext5` | 44 | 4 (9%) |
| `testtext3` ("add a warning") | 177 | 5 (3%) |
| total | 505 | 76 (15%) |

`testtext3` is the control: its goal asks to add a log line, but the *defect*
(a silent swallow) is visible in the code, so gate 1 can answer. `testtext`'s
goal asks for a missing test, which is by construction not in the code shown.

This is a genuine contract question, not a bug, and loosening gate 1 is exactly
how an 88% noise floor comes back. Ticket **`L3`** is therefore
**measure-only**: no prompt change until the numbers are in.

---

## 4. `testtext7` is running blind and nothing said so

```
probe_config  usable=False  reason=no_artifact
```

But the artifact is on disk:

```
testtext7/.collect/artifact.json   2.28 MB   Sep 9 00:11
newest tracked source              tests/test_competition_merge_csv.py   Sep 9 23:13
```

It is **stale**, not absent — and stale is correctly treated as unusable
(`CollectBridge` only ever consults a `fresh` model; the config comment at
`[collect] staleness` says so explicitly). The defect is the **reason string**:
`no_artifact` for an artifact that exists. The operator has no way to tell a
missing artifact from one that a `--collect --refresh` would fix.

Consequence for this round: `testtext7` produced 459 candidates — the most of
any run — with **zero** probe operations, while the other four made 763. Its
results are not comparable to the others' and nobody would know from the logs.

This is ticket **`L2`**, and it is the smallest and highest-yield item in the
whole set: a wrong label that silently invalidates competition data.

---

## 5. What the runs already measure for free

Every `probe_result` event carries these params:

```
ops  hits  misses  memo_hits  by_op  chars_used  run_chars_used
informed_facts  blind_facts
```

Aggregated over the round so far:

| run | probe ops | hits | misses | miss rate | memo hits | facts-op misses |
|---|---|---|---|---|---|---|
| `testtext` | 11 | 11 | 0 | 0.0% | 0 | 0 |
| `testtext3` | 326 | 319 | 7 | 2.1% | 3 | 7 |
| `testtext5` | 124 | 116 | 8 | 6.5% | 3 | 8 |
| `testtext6` | 312 | 303 | 9 | 2.9% | 1 | 4 |
| `testtext7` | 0 | — | — | — | — | — |

Every miss on the `facts` op is a symbol the architect asked for that the index
does not hold (`_check_regressions`, looked up twice and absent both times —
the memo already suppresses the third).

**This changes `EPIC-M`.** `M4` proposed adding runtime counters; a large part
of what `M5` wanted to diff already exists and is already in the traces. The
Tier-2 baseline can be captured **today**, from these five runs, with a reader
over `trace_*.jsonl` — no instrumentation, no new run, nothing to implement
first. `M4` shrinks to the collect-specific events (`collect_block`,
`collect_shrink`, `collect_miss`) and the gate-1 stage split.

---

## 6. What this changes in the plans

Nothing in `PLAN-v2` is retracted. Two things move, and three tickets are added.

**Moves:**

1. **The pack's first consumer should be gate 1 and the architect, not the
   coder.** v2 §3 builds the rows in `context_assembler` and wires them through
   `CollectBridge` — that stays exactly as written. What changes is where the
   result is *injected first*: the `tests_covering` line proves the shape works
   in a gate-1 prompt, and gate 1 is where 834 of the round's calls are. The
   coder wiring is unchanged and still lands; it is simply not the place the
   first measurable win comes from.
2. **`M4`/`M5` shrink.** The probe counters exist. Baseline from the current
   traces; instrument only what is genuinely missing.

**Additions** — see [`EPIC-L-live-findings.md`](EPIC-L-live-findings.md):

| id | what | measured yield | risk |
|---|---|---|---|
| `L1` | Gate-1 Stage A0: reject a candidate whose location is not an indexed source file, no LLM | 74 of 834 calls (10%) this round | low — deterministic, fail-open |
| `L2` | `probe_config` must say `stale` when the artifact exists but is stale | 1 of 5 runs invalidated silently | very low |
| `L3` | Gate-1's presence question vs "add X" goals — **measure first, change nothing** | 15% of rejections, 34% on one goal | high if implemented blind |

**Not added, deliberately:** the off-by-three citation window (§3b). 12 of 834
is below the noise this round can resolve, and a fix touches the architect's
line-range emission, which is out of scope for these epics. It is written down
in this file and that is the right place for it today.

---

## 7. Answer to the original question

> *"Is what is collected now enough to improve this too?"*

**For gate 1: yes, and it is already proven in-tree.** One collect query
(`tests_covering`) is wired into the gate-1 prompt and it is the only fact any
of the 1629 calls received. The other eight rows are the same wiring against
tables that are already in the artifact and already loaded — `V1`–`V5` are the
work, and none of it needs a producer change first.

**For the architect: yes, and it is measured.** The probe hit rate is 93–100%
where the artifact is fresh, so the facts the architect asks for are there. The
gap is that `testtext` made 11 probe ops for 451 candidates while `testtext3`
made 326 for 326 — the architect uses the probe when it thinks to, and gets
nothing when it does not. A pack row is a fact it does not have to think to ask
for.

**For the coder: unknown, and it will stay unknown for as long as the rounds
are `--dry-run`**, because the flag skips the execute phase and the coder never
runs. Proving anything about it needs one execution run or the `M5` A/B —
see `MEASURE-BEFORE-AFTER.md`.

**What is *not* enough:** 15% of rejections are a gate-contract mismatch that no
fact fixes, and 10% are candidates against files collect does not index, which
needs a filter rather than a fact. Facts are the answer to about 8% of the
rejections directly (the "already handled / intentional" bucket) plus an
unmeasured share of the 63% "cannot verify" bucket. **`M3`'s go/no-go and `M2`'s
redundancy count still decide the size of the win — this file does not replace
them, it tells you where to point them.**
