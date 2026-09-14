# RUN-5 — A technical failure in the presence check is not a rejection

**Status:** open  
**Severity:** HIGH  
**File:** `tools/auto/gate1_filter.py`  
**Symbol:** `Gate1Filter._check_presence` (≈ 1283–1612, the fail-closed return after the re-ask ladder) and `_presence_one` in `filter` (≈ 757–785, where `_is_technical_failure` already tells the two cases apart for logging only)  
**Round:** 33  
**Size:** S  
**Source:** live plan phases on `../testtext` (run `c506283620d3`) and `../testtext6` (run `4a93f240ff48`) with `sensenova-6.7-flash-lite`: 406 of 667 gate-1 replies in testtext were the empty string. After the ladder (`unparseable_retry_mode = fast`, empty reply pins the tier) the candidate is logged `REJECTED — JSON decode failed (Expecting value: line 1 column 1)`: **142 of 403 candidates (35 %)** in testtext, **95 of 375 (25 %)** in testtext6 were dropped from the plan without any model ever saying no  
**Depends on:** —  
**Also touches:** `tools/auto/run_trace.py` (one M4 counter), `agents.ini` (one new `[gate1]` key), `tests/test_gate1_*.py`  

---

## What happens today

Gate 1's presence stage asks the model "is the described issue present in
this code block?" and expects `{"verdict": "confirmed"|"rejected", …}`. The
re-ask ladder (GATE1-LEARN-1/2) handles a *garbled* reply well. An *empty*
reply is different: it is the provider returning nothing (rate limiting,
a safety filter, a `<think>` block eating the whole budget) and GATE1-LEARN-2
correctly stops raising tokens for it. But the ladder's exit is the same for
both: `return last_confirmed, last_reason` — `False`, `"JSON decode failed
…"` — and the candidate is treated exactly like one the model examined and
rejected.

`_is_technical_failure(reason)` already exists and already separates the
two for the log level (WARNING vs INFO). The verdict does not use it.

The effect on the plan is not subtle: a third of the architect's candidates
in testtext never reached the plan, the plan had 41 tasks instead of ~60,
and `scripts/trace_round_snapshot.py` shows `unparsed 511 / 883` where the
baseline had 0–1. Nothing downstream (V12/V13, M6) can be measured while
the plan's membership is decided by provider silence.

## What must change

1. **Three outcomes, not two.** `_check_presence` returns
   `(verdict, reason)` where `verdict ∈ {"confirmed", "rejected", "unknown"}`
   — or keep the bool and add a third element; your call, but every caller
   must be updated, and `unknown` is produced only when the ladder ends
   with `_is_technical_failure(reason)` true (empty reply, JSON decode
   failure, transport error). A model that answered `rejected` is still
   `rejected`.

2. **A policy key.** `[gate1] presence_unknown = keep | reject`
   (default `keep`). `keep` passes the candidate through with
   `reason = "presence unknown — provider gave no verdict after N re-asks"`;
   the reason string travels with the task so the coder prompt and the
   plan JSON show it. `reject` is today's behaviour for operators who prefer
   a small, certain plan. Document the key next to
   `unparseable_retry_mode` in `agents.ini`.

3. **Count it.** M4 counter `presence_unknown` next to `presence_reask`
   and the fail-closed counter, emitted in the gate-1 summary event, so
   `trace_round_snapshot.py` can show `unknown` as its own column instead
   of folding it into `unparsed`.

4. **Log line.** `Gate1[presence] UNKNOWN %r — %s` at WARNING (it *is* an
   anomaly), leaving `REJECTED` for real rejections so a log grep for
   `REJECTED` counts what the model actually rejected.

## Acceptance

- [ ] `tests/`: stub returns `""` for every attempt → candidate kept with
      the `presence unknown` reason under the default policy; dropped under
      `presence_unknown = reject`.
- [ ] Stub returns garbage then a valid `rejected` → candidate rejected
      (the ladder's success path is unchanged).
- [ ] Stub returns a valid `rejected` on the first call → rejected, no
      `unknown` counter increment.
- [ ] `presence_unknown` counter appears in the gate-1 summary event and
      `trace_round_snapshot.py` prints it.
- [ ] `presence_workers > 1` path (GATE1-PAR-1) carries the third outcome
      in order.
- [ ] `python3 -m pytest tests -q --timeout=180 && python3 -m pytest tests_bugfix -q --timeout=180` green (run sequentially).

## Out of scope

- Why the provider returns empty content (model-side). L3 measures the
  question shape; this ticket only stops silence from deciding the plan.
- V12/V13 (Stage A2) — they depend on M3, not on this.

## Ground rules (same as every round)

- Do not touch `CollectBridge._shrink`.
- Do not run anything against `agents_128k.ini` or any live provider.
- Do not edit `epic-tasks/`.
- One commit, no push.
