# M4 — Runtime counters: collect events + Gate-1 stage split

**Status:** open (verified against `84a24b2`, 2026-09-13 — re-checked after V6, GATE1-LEARN-1/2, GATE1-PAR-1 and the first live execution runs)  
**Severity:** HIGH  
**File:** `tools/auto/run_trace.py`  
**Symbol:** `—`  
**Round:** 19 of 27  
**Size:** M  
**Source:** `docs/collect-epics/EPIC-M-metrics.md` § M4  
**Also touches:** `tools/auto/collect_bridge.py`, `tools/auto/gate1_filter.py`, `analyze_logs.py`  

---

**Priority:** High · **Size:** M · **Files:** `tools/auto/run_trace.py`,
`tools/auto/collect_bridge.py`, `tools/auto/gate1_filter.py`, `analyze_logs.py`

Tier 0 and Tier 1 are static. Tier 2 needs the run to say what happened.
(This ticket absorbs v1's C5 and B4, which were measurement work filed as
feature work at the end of two different epics.)

### Verified against `84a24b2` (2026-09-13)

- `collect_miss` already exists: V9 emits it through `tracer.event(...,
  kind="collect_miss", params={reason, target, task_id?})` from
  `CollectBridge._miss`, with per-run counts on `bridge.collect_misses`.
  Reuse that channel for `absent` / `stale` / `unknown_module`; do not add a
  second one. `run_trace.py` is not where it lives — it is `tools/agent_trace.py`.
- `scripts/trace_round_snapshot.py` today counts a block by grepping the trace
  for the literal header `COLLECT MODEL (static facts` in the coder prompt.
  Once `collect_block` events exist, the snapshot must count those instead
  (and keep the grep as a fallback for pre-M4 traces), or the before/after
  columns are not comparable. Also: with several `trace_*.jsonl` per tree
  (every resume writes a new one) the script reads `traces[0]` only — it must
  read **all** of them, or take `--run-id`.
- **Three shrink paths now, not two.** Since V6 (`76fd4bd`) the budget reaches
  `build_collect_context_block(..., budget=, pack_enabled=)`, so the L6 row
  cut happens *inside the assembler* and `context_for` sees a `raw` that is
  already ≤ budget most of the time (live: every block 620–1024 chars under a
  1200 budget, `_shrink` never ran). `collect_shrink.path` is therefore
  `rows` (assembler cut, block fits) | `llm` (`_shrink` and `shrink_calls`
  incremented) | `truncate` (`_shrink` without an LLM). `rows_kept` /
  `rows_cut` are not observable from outside today — the assembler returns a
  plain `str`. Expose them through a **new** helper (e.g.
  `build_collect_context_block_stats(...) -> (str, stats)` that the old
  function wraps), not by changing the return type of the existing one.
  Still no edit inside `_shrink`.
- **The memo (V6) hides repeats.** `context_for` returns the memoised block
  on every round after the first, so a `collect_block` emitted only on a
  build would undercount "blocks per coder request". Emit it on every call
  with `memo_hit: bool`; the summary counts both.
- **Gate-1 stages today are `existence` / `presence` / `duplicate`**
  (`Gate1Result.stage`). There is **no** `already_safe` stage — that is V12's
  `is_candidate_already_safe`, unlanded; print it only if V12 exists, do not
  invent a zero column for it. What the live runs showed we *do* need is the
  presence stage split by **how** it ended (GATE1-LEARN-2 fast mode):
  `presence_confirmed`, `presence_rejected` (the model said so),
  `presence_fail_closed` (empty/unparseable after the ladder — `reason`
  starts with `JSON decode failed`, `expected JSON object` or `LLM call
  failed`), and `presence_reask` (answered only at the 0.1 re-ask). Live on
  `84a24b2`: testtext 142 of 403 candidates fail-closed (35 %), testtext6 95
  of 375 (25 %) — invisible in `gate1 accepted=N rejected=M` today.
- One more free counter, because L1 is still open: gate-1 LLM requests whose
  `cited_location.file` is not `.py` (`non_py=N`; live 40 and 31). L1 will
  drive it to 0 and needs the number to prove it.

### Do

1. Four collect events, structured like the existing ones:

   | event | fields |
   |---|---|
   | `collect_block` | `task_id`, `target_file`, `chars`, `rows_kept`, `rows_cut`, `memo_hit` |
   | `collect_shrink` | `task_id`, `target_file`, `before`, `after`, `path` = `rows` \| `llm` \| `truncate` |
   | `collect_miss` | `task_id`, `target_file`, `reason` = `absent` \| `stale` \| `dirty` \| `unknown_module` |
   | `collect_summary` | per run: blocks injected, chars injected, shrink calls, misses |

   `collect_shrink` observes `_shrink` **from the outside** — from `context_for`,
   by comparing lengths and reading the existing `shrink_calls`. It does not
   modify `_shrink`.

2. `plan_phase: gate1 accepted=N rejected=M` (`tools/auto/pipeline.py:415`)
   gains the per-stage split:
   `existence=… presence_confirmed=… presence_rejected=… presence_fail_closed=…
   presence_reask=… duplicate=… non_py=…`. Every field is always printed
   (zero when the mode does not produce it), so log parsing is stable across
   `unparseable_retry_mode` and across L1 landing.

3. `analyze_logs.py` prints a **collect** section next to its existing probe
   section:

```
collect   blocks 12/14 coder calls (memo 8) · 6 431 chars · shrink 1 (rows) · miss 2 (dirty)
probe     requests 9 · hits 7 · misses 2 · declined 0 · escalated 1
gate1     accepted 4 · existence 3 · presence rejected 5 / fail-closed 2 / re-ask 3 · duplicate 1 · non-py 4
```

### Acceptance

- [ ] `--dry-run` never reaches the coder, so it cannot emit `collect_block`
      — the acceptance is a unit test that drives `CollectBridge.context_for`
      (build, memo hit, over-budget) with a recording tracer and asserts all
      four events plus the summary; and one that runs `Gate1Filter.filter`
      against a stubbed `request_completion` returning confirmed / rejected /
      `""` and asserts the split line.
- [ ] `scripts/trace_round_snapshot.py` counts `collect_block` events when
      present, falls back to the header grep otherwise, and reads every
      trace file of a tree.
- [ ] `git diff` touches no line inside `CollectBridge._shrink`.
- [ ] Zero events and an unchanged log shape when no bridge is wired in.
- [ ] New: `tests/test_run_trace_collect_events.py`.

---

## Ground rules — these are the scoring criteria

1. **Verify before implementing.** Grep the live source for what this ticket
   claims. It was written against `competition` at `68b78a0`; if the code has
   moved, the ticket is the stale one — say so and adapt rather than forcing it.
2. **`CollectBridge._shrink` is not to be modified, reordered, or replaced.**
   New work runs *before* it, never instead of it. A diff that touches a line
   inside `_shrink` fails this round regardless of what else it does.
3. **One local commit for this ticket alone.** Never `git push`. No
   opportunistic refactors bundled in.
4. **Ships a test.** New behaviour → `tests/`; a fix to something that used to
   be wrong → `tests_bugfix/`. If you add a `tests/test_*.py`, run
   `python3 scripts/sync_test_tiers.py` so the `.smoke_tests/` mirror exists
   (a pre-commit hook enforces it).
5. **Run the two real roots, one after the other, never combined** —
   `.smoke_tests/` and `.regression_tests/` are symlink views onto `tests/`
   and run nothing extra; combining roots in one `pytest` call produces ~362
   false errors from a conftest collision:
   ```bash
   python3 -m pytest tests -q --timeout=180 && python3 -m pytest tests_bugfix -q --timeout=180
   ```
   `python` is not on PATH — use `python3`. One suite at a time — the machine
   is shared with the other round entrants.
6. **Fail open.** A broken artifact, a malformed config key or an absent model
   degrades to "no collect data" and never raises into a run. Everything added
   here inherits that.
7. **Never widen provenance.** Static facts and LLM prose stay separately
   labelled. `is_safe()` keeps reading only `guarded_accesses` /
   `FAIL_OPEN_REGISTRY` / `CONTRACTS`.
8. **Never run anything against a live provider config.** Copy
   `agents_128k.ini` to a scratch path and point `base_url` at a stub before any
   measurement run.
