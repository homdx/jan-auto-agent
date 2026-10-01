# RUN-5 — A technical failure in the presence check is not a rejection

**Status:** landed — the commit after `24d9028` (RUN-5; base `ade28e6`/`24d9028`). Competition (6 entrants in `run5/`; reviewer probe = the 5 ACs driven through the real `Gate1Filter.filter()` with a stubbed provider — all-empty under `keep` and under `reject`, garbage-then-`rejected`, `rejected` on the first call, transport error under both policies, `presence_workers = 3` with a mixed batch consumed in order, `presence_unknown` next to a still-present `presence_fail_closed` in `_GATE1_SPLIT_FIELDS` and in `format_gate1_split`, a malformed key falling back to `keep`, the kept result carrying a *presence unknown* reason — 9 probes; then the entrant's own tests, `tests`, `tests_bugfix`, `sync_test_tiers --check`): **Sensenova6-8-var1 — winner, base of the ideal** (9/9, both suites green, 26 tests in `tests/test_gate1_presence_unknown.py` + smoke mirror; bool + third element from `_check_presence`, one `_finish_presence` exit so every path — first call, ladder exhausted, ladder call raising, transport error — is classified once; a live thread-safe `presence_unknown` counter like `presence_reask` (a kept-unknown later dropped as a duplicate leaves no record); the kept reason keeps the cause: *presence unknown — provider gave no verdict after N re-ask(s): JSON decode failed …*; `_is_technical_failure` matches the prefix so AUTO-REMOVE-GUARD-1 leaves a `reject`-policy drop untouched in `--validate-plan`; `trace_round_snapshot.py` reads `unknown` from the `gate1_split` event, own column; presence summary line says *(N without a verdict)*); HY3 — 9/9, green, 13 tests: verdict string, `_verdict_for` on every exit, agents.ini key documented but commented out (`;`), `presence_fail_closed` goes dead (every unknown lands in the new column, including under `reject`) and the ladder-call-raised exit returns `unknown` without the prefix so it is counted as fail-closed after all; routes a kept-unknown to `inconclusive` in `plan_validator` via a private `c._presence_unknown` attribute — the right report, the wrong carrier; Sensenova6-8-var3 — 9/9, green, 15 tests: the counter is bumped on two of the three unknown exits (the ladder-call-raised path is missed), the kept reason has no N, under `reject` the drop is counted in both `presence_fail_closed` and `presence_unknown`; Sensenova6-8-var2 — 9/9, green, 14 tests: kept reason says *after `unparseable_max_retries` re-asks* (the configured maximum, not the number made — wrong for a transport error), double count under `reject`, `_accepted_reasons` dict threaded through `filter()`/`filter_candidates()` for `plan_validator` (explicit, but an API for one consumer); Sensenova2-1 — 9/9, green, but 1 test (`presence unknown` is technical) — none of the five acceptance tests exist; drops the cause from the reason (*after 0 re-ask(s)* for a transport error); code otherwise clean; Step3-7-Flash — 7/9: **removes `presence_fail_closed` from the M4 split** (a column every snapshot since M4 has), renames the snapshot's per-reply `unparsed` to `unknown` (two different things), reason text differs per exit; a `FilterResult.verdict` field is a fine carrier and `plan_validator` honours it. Taken beyond the winner: the reason travels with the task — `_with_presence_unknown_note` appends *Gate 1 note: presence unknown …* to the kept candidate's `instruction` once (idempotent across a `--validate-plan` re-check), so plan.json and the coder prompt show it; 3 tests. Not taken: the `inconclusive` routing of a kept-unknown in `--validate-plan` (HY3/var2) — under `keep` the task is left untouched either way, only the report's *confirmed still needed* count is optimistic; noted below. `agents_128k.ini` gains `presence_unknown = keep`.  
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
- [ ] `python3 -m pytest tests -n 4 -q --timeout=180 && python3 -m pytest tests_bugfix -n 4 -q --timeout=180` green (run sequentially).

## Out of scope

- `--validate-plan` under `keep`: a task whose re-check ends `unknown` is
  reported as *confirmed still needed* (it is left untouched, which is the
  right action; the count is optimistic). Routing it to `inconclusive` needs
  the accepted reason to leave `filter()` — a later ticket, if the count
  matters.

- Why the provider returns empty content (model-side). L3 measures the
  question shape; this ticket only stops silence from deciding the plan.
- V12/V13 (Stage A2) — they depend on M3, not on this.

## Ground rules (same as every round)

- Do not touch `CollectBridge._shrink`.
- Do not run anything against `agents_128k.ini` or any live provider.
- Do not edit `epic-tasks/`.
- One commit, no push.
