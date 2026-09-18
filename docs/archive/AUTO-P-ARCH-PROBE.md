# AUTO-P — Architect context probe (pull-model planning)

**Status:** proposed
**Branch target:** `feature/arch-probe`
**Revision:** v2 — checkpoint work dropped; the H5 collision promoted to its
own ticket; Phase 0 trimmed to a spike.
**Prerequisite reading:** `tools/auto/architect.py`, `tools/auto/coder.py`
(`_extract_context_request` / `_fetch_needed`), `tools/auto/collect_bridge.py`

---

## 1. Problem

The Architect (`ClusterReviewer._review_one_cluster`) plans against a
one-shot, fixed-size slice of the repository. With today's `agents.ini`:

```ini
[architect]
max_files_per_review = 4
max_file_chars       = 2500
max_tokens           = 768
```

that is **≤ 10 KB of source per LLM call**, against ~82 source modules and
~250 test files. The Architect cannot tell whether a test for the change
already exists, whether the symbol it wants to touch has three other
callers, or whether the `acceptance_check` it is about to emit already
passes. It has no channel to ask. It must guess.

The rest of the pipeline exists to catch the consequences of those guesses
after the fact — Gate-1 grounding, `existence_validator.py`,
`fact_validator.py`, `delta_validator.py`. That works, but it spends a full
plan→gate cycle discovering what one cheap lookup would have settled.

The two retry ladders already in `_review_one_cluster` do not help, because
both react to the **shape of the response**, not to a shortfall in
knowledge:

| Ladder | Trigger | Remedy | Config |
|---|---|---|---|
| AUTO-H4 | JSON array cut off mid-object, ≥1 task salvaged | halve `max_tasks`, re-ask | `truncation_retry_max` |
| AUTO-H5 / ESCALATE-1 | response unsalvageable (empty, degenerate, non-JSON) | escalate `max_tokens`/`temperature`, re-ask | `empty_response_retry_max` |

Neither lets the model say *"I need to see X before I can plan this."*

## 2. Prior art — inside this repository

The pull model is not new here. It exists twice, and both implementations
bypass the Architect.

**`tools/auto/coder.py` — in-generate context probe.** The Coder's reply is
scanned for a trailing `CONTEXT_REQUEST: name1, name2` line;
`_fetch_needed()` resolves those names via `SearchAgent`; the blocks are
appended to `messages[-1]` as `## Fetched context (requested)` and the same
payload is streamed a second time. Gated by `[coder] context_probe`,
budgeted by `max_chars_per_dep` / `max_total_dep_chars`, capped at one
extra round.

**`tools/auto/context_broker.py` — pull for Gate-2.** On `missing_context`,
resolves names through `block_extractor`, then a project scan, then
`CollectBridge.pull_symbol` as Pass 3.

**`tools/auto/collect_bridge.py` — structural facts.** `pull_symbol`,
`contracts_for_symbol`, `tests_covering` answer from the collect model's
AST facts rather than raw source. Denser and more reliable than grep, built
once per run, already wired behind `[collect] use_in_auto`.

**Precedent for a bounded planning loop.** `pipeline.py::_run_plan_phase`
already re-runs the Architect with feedback up to
`[architect] plan_max_revisions`, failing open at the cap. AUTO-P is the
same shape one level down.

Most of what this epic needs is written and tested. The work is wiring,
budgeting, and a decision loop.

## 3. What the reference implementations do

Claude Code, OpenCode and Kilo all run a ReAct loop: the model emits either
tool-use blocks or a final answer; tool results are appended and the loop
repeats. Four properties matter here.

**Narrow tools beat a bash escape hatch.** Read / Glob / Grep are preferred
over shelling out — a narrow tool has a smaller failure surface and returns
structured output both the harness and the model can reason over.

**Read-only is separated from state-changing.** Plan mode in Claude Code
and OpenCode is exactly a read-only research phase with writes disabled.

**Isolated context, summarized return.** Subagents get their own history
and return a digest, not their transcript.

**Hard turn budgets are mandatory,** set low and raised deliberately.
Research on agentic abstention (arXiv 2606.28733) finds the hard part is
not *whether* agents stop but *when* — some never abstain, others only
after many wasted interactions, and larger models are sometimes worse at
it. Corollary: **the give-up decision belongs to the harness, not the
model.**

## 4. Design

### 4.1 Protocol

Text protocol, not native tool-calling. `tools/llm_stream.py` has **no**
`tools=` / `tool_call` support today (verified); adding it would mean a
migration across `api_format = ollama`, the stub servers, and seven ini
profiles. The `CONTEXT_REQUEST:` convention in `coder.py` is the
established in-repo pattern and costs nothing to extend.

The Architect may reply with **either** the JSON task array (today's
contract, unchanged) **or** a single line:

```
ARCH_PROBE: facts <name>, facts <name>
```

| Op | Backed by | Returns | Phase |
|---|---|---|---|
| `facts <symbol>` | `CollectBridge.pull_symbol` / `contracts_for_symbol` / `tests_covering` | signature, contracts, covering tests | 0 |
| `symbol <name> [file]` | `block_extractor.extract_block` | full source block | 1 |
| `refs <name>` | `SearchAgent` reference scan | call sites, file+line | 1 |
| `read <path> [a:b]` | `file_reader` + `get_context_lines` | line range | 1 |
| `testcheck <cmd>` | `Executor.run_raw` + allowlist | exit code, output tail | 2, off by default |

`facts` first, deliberately: densest per token, no new extraction code, and
`CollectBridge` already exists per run.

### 4.2 Loop

```
probe_rounds_used = 0
digest = ""
loop:
    response = LLM(prompt + digest)
    if response is a valid JSON task array   -> parse, done
    if response is an ARCH_PROBE line:
        if probe_rounds_used >= probe_max_rounds
           or digest_chars >= probe_max_total_chars:
               break to FORCED
        execute ops (read-only), append truncated result to digest
        probe_rounds_used += 1
        continue
    otherwise -> existing AUTO-H4 / AUTO-H5 ladders, unchanged

FORCED:
    one final call with "no further probes; plan from what you have"
    if that still yields nothing -> investigation ticket
```

The **forced final call is not optional**. Without it, hitting the cap
means zero candidates from the batch — a regression against today.

### 4.3 Prompt injection is additive

Probe instructions are appended to `user_msg` **only when `probe_enabled`
is true**. `_USER_PROMPT_TMPL` and `_USER_PROMPT_CREATIVE` are not edited.
With the feature off, every prompt is byte-identical to today's — the
default path is provably unchanged and prompt-sensitive tests stay green.

### 4.4 Budgets

Two caps, both fail-safe:

* `probe_max_rounds` — probe iterations per batch (Phase 0: **1**).
* `probe_max_total_chars` — accumulated digest per batch. Reached first in
  practice; the reason a runaway op cannot blow the window.

Per-op results are hard-truncated with a visible notice. No LLM-based
compaction in Phase 0 — with one round it would be a summarizer call to
save a summarizer call.

### 4.5 Checkpointing — deliberately untouched

`review_clusters()` computes `batch_key` **before** the LLM call, so a
probe transcript cannot enter the lookup key anyway. Earlier drafts added a
`||probe` marker to invalidate the cache on flag flips. **Dropped.**

The real exposure is not correctness — a probe-derived plan restored from
cache is still a valid plan. It is *measurement*: turn `probe_enabled` on,
find every batch is a checkpoint hit from an earlier probe-off run, watch
the probe never fire, and conclude from clean-looking metrics that nobody
wants the feature.

Fix is operational, not code:

> **Runbook:** delete `.agent/architect_checkpoint.json` when flipping
> `probe_enabled`, or the decision-gate metrics are measured against cached
> pre-probe plans.

Losing a checkpoint on restart costs one re-review of the in-flight batch.
That is cheaper than the code, the config key, and the test needed to avoid
it.

### 4.6 Abstention

The give-up decision is the harness's, by counter — never the model's (§3).
On cap exhaustion with no usable plan,
`ticket_store.make_ticket(type="investigation", linked_task="")` records
what was asked for and what could not be answered. Same mechanism
`exhaustion_handler.py` uses for tasks that burn all their rounds.
**Phase 1** — Phase 0 just logs.

### 4.7 Deliberately out of scope

| Not doing | Why |
|---|---|
| Native tool-calling in `llm_stream.py` | migration across 3 API formats, stub servers, 7 ini profiles; no payoff until the loop is proven useful |
| Free-form bash for the Architect | LLM-generated shell in the *plan* phase, upstream of every gate. Read-only ops cover ~90% of "I lack knowledge". `Executor._check_command_safety` is a best-effort token scan, explicitly not a sandbox |
| Checkpoint invalidation | §4.5 |
| Editing the other six `agents_*.ini` | absent key ⇒ `fallback=False` ⇒ feature off. Nothing to add |
| LLM digest compaction | one round, hard truncation is sufficient |
| Parallel op execution | ops are cheap and local; ordering keeps the trace readable |

---

## 5. Epic breakdown

### Phase 0 — spike (build this, then stop and measure)

Three tickets. One new module, one diff, feature off by default.

---

#### AUTO-P1 — `facts` probe, one round, opt-in

| | |
|---|---|
| **New** | `tools/auto/arch_probe.py` |
| **Modified** | `tools/auto/architect.py` (`__init__`, `_review_one_cluster`), `agents.ini` |
| **Size** | ~200 LOC new, ~50 LOC diff |

Public surface:

```python
PROBE_PREFIX = "ARCH_PROBE:"

@dataclass
class ProbeOp:
    op: str          # "facts"
    arg: str

def extract_probe_request(text: str) -> list[ProbeOp]:
    """Parse a trailing ARCH_PROBE: line. [] when absent or malformed.
    Mirrors coder.py::_extract_context_request — dedup, order-preserving,
    capped at _MAX_OPS."""

class ArchProbe:
    def __init__(self, collect_bridge, *, max_chars: int,
                 max_total_chars: int): ...
    def execute(self, ops: list[ProbeOp]) -> str:
        """Run read-only ops, return a prompt-ready digest. Fail-open: any
        op that raises or misses contributes '(not found)', never an
        exception."""
    @property
    def chars_used(self) -> int: ...

PROBE_INSTRUCTIONS: str   # appended to user_msg only when enabled
FORCED_SUFFIX: str        # appended on the final, no-more-probes call
```

Behaviour contract:

1. `probe_enabled = false` → zero behaviour change, byte-identical prompts.
2. `collect_bridge` is `None` or not `usable` → instructions are **not**
   appended; there is nothing to answer with.
3. Every failure path degrades to today's behaviour.

Trace wiring (three lines, no schema change — `view_trace.py` and
`analyze_logs.py` read it as-is):

```python
tracer.event(source="architect", target="probe", kind="probe_request",  ...)
tracer.event(source="probe", target="architect", kind="probe_result",   ...)
```

**Tests — `tests/test_auto_p1_arch_probe.py`** (fast tier; all LLM patched,
no network, no real sleep — follows `test_auto_h5_empty_response_retry.py`):

| AC | Assertion |
|---|---|
| AC-P1-1 | `probe_enabled=false` → prompt byte-identical to pre-change; exactly one LLM call |
| AC-P1-2 | `ARCH_PROBE: facts X` → second call made, `## Probe results` present, `facts X` content present |
| AC-P1-3 | Second call returns valid JSON → those candidates are used |
| AC-P1-4 | Second call returns another `ARCH_PROBE:` → cap reached, forced final call carries `FORCED_SUFFIX`, no third probe |
| AC-P1-5 | Forced final call yields nothing → 0 candidates, **not** an exception, batch **not** checkpointed |
| AC-P1-6 | `collect_bridge=None` → `PROBE_INSTRUCTIONS` absent from the prompt, single call |
| AC-P1-7 | Op result over `probe_max_chars` is truncated with a visible notice |
| AC-P1-8 | `probe_max_total_chars` wins over `probe_max_rounds` when reached first |
| AC-P1-9 | `ArchProbe.execute` never raises when the bridge is monkeypatched to throw |

**Tests — `tests/test_auto_p1_probe_parser.py`** (pure unit, no LLM):
op parsing, dedup, order preservation, `_MAX_OPS` cap, whitespace and case
tolerance, prefix mid-response vs on its own trailing line, unknown op,
empty arg.

**Registration:** neither file goes in `tests/SLOW_TESTS.txt`; run
`python3 scripts/sync_test_tiers.py` and commit the regenerated symlinks.

---

#### AUTO-P2 — probe response must not consume an H4/H5 retry

**This is the ticket that makes or breaks the feature.** Split out of
AUTO-P1 so it cannot be quietly folded into a passing test run.

Not a defect in existing code — `ARCH_PROBE` does not exist yet, so nothing
is broken today. It is the failure mode of the naive implementation, and it
is silent: an `ARCH_PROBE:` line is non-JSON prose, so
`_parse_candidates_ex()` classifies it **unsalvageable** and the AUTO-H5
ladder fires — re-asking the same question at rising `max_tokens` and
temperature, up to six times, while ignoring the request the model actually
made. Logs look like a flaky model. The feature appears to do nothing.

| | |
|---|---|
| **Modified** | `tools/auto/architect.py` — `_call_and_parse` closure and its two call sites |
| **Size** | ~15 LOC |

`_call_and_parse` returns a 4-tuple instead of a 3-tuple:

```python
(candidates, truncated, unsalvageable, probe_request)
```

**Invariant: when `probe_request` is non-empty, `unsalvageable` is forced
to `False`.** The `while candidates is not None and (truncated or
unsalvageable)` loop gains a probe branch ahead of both ladder branches.

`_call_and_parse` is a closure inside `_review_one_cluster`; existing H4/H5
tests drive `_review_one_cluster` / `review_clusters` from outside with a
patched LLM, so the arity change is internal and does not touch them.

**Tests — `tests/test_auto_p2_probe_ladder_isolation.py`:**

| AC | Assertion |
|---|---|
| AC-P2-1 | A probe response consumes **no** H5 retry — drive a probe, then a genuine unsalvageable response, and assert the full `empty_response_retry_max` budget is still available (call count) |
| AC-P2-2 | A probe response consumes **no** H4 shrink — `max_tasks` unchanged across the probe round, verified from the prompt text actually sent |
| AC-P2-3 | Malformed `ARCH_PROBE:` (no ops / unknown op / empty arg) is **not** treated as a probe — falls through to AUTO-H5 normally |
| AC-P2-4 | Genuinely unsalvageable → AUTO-H5 fires exactly as before this epic (guards the 4-tuple change) |
| AC-P2-5 | Genuinely truncated → AUTO-H4 fires exactly as before |
| AC-P2-6 | Probe → truncated → unsalvageable in sequence: each handled by the right mechanism with independent budgets (mirrors AC-H5-8) |
| AC-P2-7 | `probe_enabled=false` → a response that *happens* to contain the string `ARCH_PROBE:` is treated as unsalvageable, not as a probe |

AC-P2-4 and AC-P2-5 exist because AUTO-P2 changes a function signature the
two existing ladders depend on. They are the regression net for that
change, not new behaviour.

---

#### AUTO-P3 — config keys and startup lint

| | |
|---|---|
| **Modified** | `agents.ini`, `tools/auto/controller.py` (`_lint_mode_config`) |
| **Size** | ~25 LOC |

Only `agents.ini` — the other six profiles need nothing, since an absent
key resolves to `fallback=False`.

Warn at startup when:
* `probe_enabled = true` while `[collect] use_in_auto = false` — the probe
  has no data source;
* `probe_enabled = true` while `num_ctx` is below
  `_CR19_SMALL_NUM_CTX_THRESHOLD` (8192) — no room for a probe round.

**Tests — extend `tests/test_cr19_config_lint.py`:** one case per warning,
plus one asserting silence in the healthy configuration.

---

### Decision gate — measure before building Phase 1

Delete `.agent/architect_checkpoint.json`, set `probe_enabled = true`, run
a representative set of goals. Count from the trace (`view_trace.py`, or a
ten-line `grep` over `probe_request` events):

| Metric | Meaning | Threshold to proceed |
|---|---|---|
| probe request rate | batches emitting `ARCH_PROBE` ÷ batches reviewed | **≥ 10%** |
| Gate-1 grounding rejection rate | before vs after, same goals | measurable drop |
| candidates per cluster | before vs after | no collapse |
| wall-clock per plan phase | before vs after | ≤ +30% |
| forced-final rate | batches that hit the cap | **< 30%** (higher ⇒ the cap or the op set is wrong, not the idea) |

**If the request rate is under 10%, stop here.** Phase 0 is
self-contained, already useful, and costs nothing when off. Building the
rest would be work in search of a problem — which is exactly what this gate
exists to find out cheaply.

---

### Phase 1 — full loop (only if the gate passes)

| ID | Title | Files | Tests | Size |
|---|---|---|---|---|
| AUTO-P4 | Multi-round loop, `probe_max_rounds` up to 3; digest accumulation across rounds | `arch_probe.py`, `architect.py` | `test_auto_p4_multi_round.py` — exactly N rounds; total-char cap wins over round cap; digest grows monotonically; round counter in trace | M |
| AUTO-P5 | `symbol` / `refs` / `read` ops | `arch_probe.py` | `test_auto_p5_probe_ops.py` — per op: hit, miss, oversized result, path outside `base_dir` rejected, binary file rejected | M |
| AUTO-P6 | Abstention → investigation ticket on cap exhaustion with no plan | `architect.py` (`ticket_store` consumer only) | `test_auto_p6_probe_abstention.py` — ticket carries the op list; **no** ticket when the forced call succeeds; id collision handled | S |
| AUTO-P7 | Stub-server scripted sequences (`probe → JSON`, `probe → probe → forced`) | `emulate_stub_server_stub_llm.py`, `stub-test/` | `test_auto_p7_stub_probe_sequence.py` — end-to-end to Gate-1, both scenarios | S |
| AUTO-P8 | Shared LLM-call ceiling across H4 / H5 / probe (`probe_max_llm_calls_total`) | `architect.py` | `test_auto_p8_call_budget.py` — worst-case interleaving never exceeds the ceiling; `0` disables the shared cap | M |

AUTO-P8 only matters once `probe_max_rounds > 1`: with 3 rounds, 2 shrink
retries and 6 empty retries, the theoretical worst case is 36 calls per
batch. At Phase 0's single round the worst case is +1 call, which is why
the shared ceiling is not in Phase 0.

### Phase 2 — speculative, do not schedule

| ID | Title | Precondition |
|---|---|---|
| AUTO-P9 | `testcheck` op — `Executor.run_raw` behind a strict allowlist (`pytest`, `grep -q`, `python -c`), own flag, default off, workspace-isolated | Phase 1 shipped and a real case exists where read-only ops were insufficient |
| AUTO-P10 | LLM digest compaction (reuse the `CollectBridge._shrink` pattern) | multi-round digests routinely hit `probe_max_total_chars` |
| AUTO-P11 | `analyze_logs.py` probe report — round histogram, op frequency, forced-final rate | probe is permanently on and the trace grep stops scaling |
| AUTO-P12 | Native tool-calling in `llm_stream.py` for `api_format = openai` | text-protocol parse failures appear in the metrics |

---

## 5a. The facts epic (AUTO-F) — built on `pullv3`

Separate epic doc (`AUTO-F: raise the facts hit rate from 43%`), depends on
AUTO-P11 (`0d8e188`, merged). Where §5 above got the probe loop running at
all, this epic is about one op inside it: a measured run (run 13) put the
`facts` hit rate at 43% (60/140) against 95%+ for `module` and `read`, and
falling as the epic progressed. Full background, the run-13 trace, and the
class A/B/C miss breakdown live in the epic doc, not repeated here — this
section documents what actually shipped: **AUTO-F1 and AUTO-F4/F4a/F4b.**
AUTO-F2 ("did you mean") and AUTO-F3 (resolver call) are still proposed
only; see the note at the end of this section.

---

#### AUTO-F1 — run-level miss memo

| | |
|---|---|
| **Modified** | `tools/auto/arch_probe.py`, `tools/auto/architect.py`, `agents.ini` |
| **Size** | 300 insertions across 5 files |

`ArchProbe` remembers every `(op, arg)` that missed during the run. A
repeated miss is answered from the memo — **no bridge lookup** — with an
escalating message:

```
(not found — already looked up 12 times this run, still absent.
`retry` is not a symbol in this repository. If you mean a file, use
`module <path>`; if you mean a concept, name a real function.)
```

Scoped to the run; `reset()` deliberately does **not** clear it — that is
the entire point (the collect artifact is immutable for the run, so a name
dead at batch 3 cannot resolve at batch 40). A memo hit still counts as a
miss in `by_op`, never a hit, so it cannot flatter the metric it is meant
to fix.

**Tests — `tests/test_auto_f1_miss_memo.py` (176 LOC):**

| AC | Assertion |
|---|---|
| AC-F1-1 | Repeated miss on the same `(op, arg)` performs no bridge lookup |
| AC-F1-2 | The message names the repetition count |
| AC-F1-3 | Memo is per-run: a fresh `ArchProbe` starts empty |
| AC-F1-4 | `reset()` does **not** clear the memo |
| AC-F1-5 | Memo hits count as misses in `by_op`, never as hits |
| AC-F1-6 | A `probe_memo_hit` counter reaches the trace |
| AC-F1-7 | A hit is never memoised — only misses |
| AC-F1-8 | Bounded at `probe_memo_max_entries` (default 200), oldest-first eviction |

**Measured/expected effect:** ~45 fewer wasted lookups on run 13's numbers;
43% → ~63% hit rate from this ticket alone, no extra LLM call.

---

#### AUTO-F4 — teach the `module → facts` sequence, and measure it

| | |
|---|---|
| **Modified** | `tools/auto/arch_probe.py`, `tools/auto/architect.py`, `analyze_logs.py` |
| **Size** | 582 insertions across 5 files |

`PROBE_INSTRUCTIONS` forbade guessing but never said what to do instead.
Added:

```
If you do not KNOW a symbol's exact name, do not guess it. Ask
`module <path>` first — it lists every top-level name in that file
with its line number — then ask `facts` for one of the names it
returned.
```

Because instructions in this codebase have already been shown to get
ignored — that is the reason AUTO-F1 exists — this ships with its own
adherence metric instead of assuming the sentence works: a `facts` ask is
**informed** when its name appeared in a `module` result earlier in the
same batch, **blind** otherwise. No behaviour change — a blind ask still
runs exactly as before; this ticket measures, it does not restrict.

Mechanically: `ArchProbe._learn_module_names()` scans a `module` hit's
result for `  name(...)` lines and adds each to `_informed_symbols` for
the batch (wiped on `reset()`). A `facts` ask is classified against that
set **before** its own lookup runs — memo hit, bridge hit, or miss all
still happen exactly as before. `probe_result` carries `informed_facts` /
`blind_facts` as `"hits/asks"` strings; `analyze_logs.py` keeps the latest
(cumulative) pair per cluster and sums across the run, printed once per
run only when `facts` was ever asked.

**Tests — `tests/test_auto_f4_informed_facts.py` (380 LOC):**

| AC | Assertion |
|---|---|
| AC-F4-1 | Instructions state the sequence, not only the prohibition |
| AC-F4-2 | `probe_result` carries `informed_facts` / `blind_facts` |
| AC-F4-3 | `analyze_logs` reports the split whenever `facts` was asked |
| AC-F4-4 | Hit rate reported separately for informed vs. blind |
| AC-F4-5 | No behaviour change — a blind `facts` still runs |

Plus one regression case not in the original epic doc, added during
implementation: a name that exists in a `module` result but is truncated
out of the digest by the per-op char cap (`ArchProbe._cap`) must **not**
be learned — `_learn_module_names` reads the capped text `_format_block`
actually sends onward, not the raw bridge response.

---

#### AUTO-F4a / AUTO-F4b — the split misses declined rounds

| | |
|---|---|
| **Modified** | `tools/auto/architect.py`, `analyze_logs.py` |
| **Size** | F4a: 379 insertions / 4 files. F4b: 115 insertions, 36 deletions / 2 files |

AUTO-F4 put `informed_facts`/`blind_facts` on `probe_result`. It missed the
hole AUTO-P6 (`4520393`) already had to close once for the general `by_op`
tally: an all-miss round emits **no** `probe_result` — it emits
`probe_declined` instead (AUTO-P4b). Any `facts` asks inside such a round
were tallied in memory but never reached an event, so the reported split
silently under-counted.

F4a's first cut copied `_decline_by_op`'s shape — `"0/0"` for every decline
reason except `unresolved`. That is correct for `by_op` (a **per-round**
delta that analyze_logs sums), but wrong here: `informed_facts`/
`blind_facts` is a **cumulative-per-batch snapshot**, and analyze_logs
keeps only the *last* value per cluster, not a sum. A hardcoded `"0/0"` on
a trailing `repeat` or other non-`unresolved` decline didn't just fail to
add anything — it **overwrote and erased** whatever real counts earlier
rounds in the same batch had already established, whenever that decline
happened to be the last event recorded for its cluster.

A live run (`trace_47f94038c230`) hit this: two batches' facts data — one
of them 1 informed hit plus 3 blind misses — vanished behind a trailing
`repeat` decline, undercounting that run's `facts` denominator by 5 and its
hit count by 2. **F4b** fixed it: always read the live cumulative counters
regardless of decline reason; `"0/0"` only when `_probe` itself is `None`
(`no_executor` — nothing was ever built to have state).

**Tests — `tests/test_auto_f4a_declined_facts_seq.py`:**

| AC | Assertion |
|---|---|
| AC-F4a-1 | An `unresolved` decline with a blind miss carries a real `blind_facts` count |
| AC-F4a-2 | An `unresolved` decline with an informed-but-missed ask carries a real `informed_facts` count |
| AC-F4a-3 | **Every** decline reason with a live `_probe` reports its true cumulative split — including `round_cap` and `repeat`, which must not erase real prior data. Only `no_executor` genuinely has nothing |
| AC-F4a-4 | `analyze_logs` includes a declined round's contribution in the rendered split |
| AC-F4a-5 | A pre-AUTO-F4a trace (no `informed_facts`/`blind_facts` on `probe_declined`) still renders without crashing |
| AC-F4a-6 | Totals sum correctly across clusters when one ends in a decline and another in an ordinary result |

---

#### Not built: AUTO-F2, AUTO-F3

The epic doc also proposes a `difflib` "did you mean" suggestion on a
near-miss (**AUTO-F2**, config `probe_suggest_threshold` / `probe_suggest_max`)
and a small, bounded resolver LLM call as a last resort (**AUTO-F3**, config
`probe_resolver_enabled` / `probe_resolver_max_per_batch` /
`probe_resolver_candidates`). Neither has landed on any branch as of this
writing — `grep -r "probe_suggest\|probe_resolver"` across the tree returns
nothing. The epic's own rollout order has F4 landing *before* F3
deliberately ("its numbers decide whether F3 is worth turning on"); F2 has
no such dependency and simply appears not yet started.

---

## 6. Config reference

```ini
[architect]
# ── AUTO-P: architect context probe ──────────────────────────────────────
# Lets the Architect ask for facts it is missing instead of guessing.
# OFF by default: the 4k/32k-fast profiles have no room for a probe round,
# and with this false every prompt is byte-identical to pre-AUTO-P.
# Requires [collect] use_in_auto = true — that is the data source.
# NOTE: delete .agent/architect_checkpoint.json when flipping this, or
# cached pre-probe plans will be replayed and the probe never fires.
probe_enabled         = false
# Probe iterations per batch before the forced final call. Phase 0 ships 1.
# Keep this low — a high cap masks a too-narrow max_files_per_review.
probe_max_rounds      = 1
# Cap on a single op's result (hard truncation, visible notice).
probe_max_chars       = 2000
# Cap on the accumulated digest per batch. Reached first in practice.
probe_max_total_chars = 6000
# Comma-separated. Phase 0 supports "facts" only.
probe_allowed_ops     = facts
# AUTO-F1: run-level memo of every (op, arg) that missed. A repeated miss is
# answered from the memo — no bridge lookup — with a digest line naming how
# many times it has already been asked. Bounded, oldest-first eviction;
# never cleared between batches within a run (only a fresh run starts empty).
probe_memo_max_entries = 200
```

---

## 7. Risk register

| Risk | Mitigation | Ticket |
|---|---|---|
| Probe response eats an H5 retry; model never gets its answer; looks like a flaky model | 4-tuple from `_call_and_parse`, `unsalvageable` forced false | **AUTO-P2** |
| The 4-tuple change breaks H4 or H5 | AC-P2-4 / AC-P2-5 regression net | **AUTO-P2** |
| Cap exhaustion silently drops a batch | forced final call is mandatory | AUTO-P1 / AC-P1-5 |
| Stale checkpoint hides the probe and corrupts the gate metrics | runbook line: delete the checkpoint on flag flip | §4.5 |
| Context overflow on small profiles | off by default; total-char cap; startup lint on low `num_ctx` | AUTO-P3 |
| Ladder multiplication → 36 calls/batch | not reachable at 1 round; shared ceiling when rounds > 1 | AUTO-P8 |
| LLM-generated shell in the plan phase | no `testcheck` before Phase 2, separate flag, default off | AUTO-P9 |
| Model probes instead of planning | forced-final-rate metric; if > 30%, the fix is `max_files_per_review`, not the cap | decision gate |
| Feature built, nobody uses it | Phase 0 is one module and one diff; the gate exists to catch this | decision gate |

---

## 8. Order of work

1. **AUTO-P2 first** — the 4-tuple and its regression net, with a stub
   `extract_probe_request` that always returns `[]`. Ladders provably
   unchanged before any new behaviour exists.
2. **AUTO-P1** — the real module and the loop. AUTO-P2's tests go green
   for the right reason.
3. **AUTO-P3** — config and lint.
4. **Stop.** Delete the checkpoint, turn it on, run real goals, read the
   metrics.
5. Phase 1 ticket by ticket, only against what the metrics justify.

Building AUTO-P2 before AUTO-P1 is deliberate: it is the change most likely
to break something that already works, and it is far easier to prove
correct against a probe parser that returns nothing than against one that
returns real ops.
