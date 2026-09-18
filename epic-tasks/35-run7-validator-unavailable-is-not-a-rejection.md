# RUN-7 — "validator unavailable" is not a rejection

**Status:** landed — the commit after `acbfd61` (RUN-7; base `acbfd61`, L1 landed). Competition (8 entrants in `run7/`; reviewer probe = the ACs driven through the real `InnerLoop` + `LLMGate2Validator` over a stubbed `request_completion` and the real `OuterLoop` over a `StateStore` — every call down; down ×2 then approved; down ×2 then `{"approved": false}`; retries = 0; exec-fail then outage; `_prior_validator_critique` across an outage; an `approve()`-only fake; fresh task left todo; a task with two failed rounds left todo at round 2 and resumed at round 3; metrics row; tuner score; `OuterLoopResult.unavailable`; controller dispatch — 26 probes; then the entrant's own tests, `tests`, `tests_bugfix`, `sync_test_tiers --check`): **Sonnet-5 — 26/26, winner and base of the ideal** (a `last_unavailable` side channel set only on `approve()`'s except exit, so the flag is the exit itself and not a prefix match on the reason; one UNAVAILABLE decision event per outage with the call count; `attempts_used = attempt − 1`; a raising validator stays a charged *validator error*; controller routes around exhaustion and relabels the metric row; `PromptEvaluator` filtered in both scoring paths; 10 tests; its one gap: the wait is a hard-coded 10 s, not `error_retry_wait_sec`); HY3 — 26/26, green (per-round round restore from the store — the only entrant that read the persisted value, taken; but `unavailable` is a `startswith("validator unavailable:")` on the reason, the wait reads `[loop] error_retry_wait_sec`, a section that does not exist, so it is always 60, `run_trace.log_task_blocked` is called for a task that is going back to todo, an unrelated first commit — L2 test — rides along); Sensenova-4-8-Flash — 25/26 (the most thorough: flag side channel, `scoreable_records` also in `evaluate()`'s baseline load, `[collect] error_retry_wait_sec` as the wait, 26 tests; but `_round_at_entry` is read once before the round loop, so a task whose rounds 1–2 failed in this session and whose round 3 hit the outage is put back to round 0 — the probe's P4b; one UNAVAILABLE event per *call*, so the snapshot column counts retries, not outages; a validator that raises is also classified unavailable; an unrelated first commit); Atria Dawn (late entry, `0002-RUN-7-…patch`; its `0001` is the M3 suppressor-ceiling ticket, off-ticket here) — 25/26 (same side-channel design as the winner, independently; `_validator_accepts_prior` inspected on the method actually called — taken; `g2 rej` / `g2 err` next to `g2 unavail` in the snapshot — taken, with its snapshot tests; a `validator_status` string override on `record_gate2` so the controller passes the kwarg only on an outage; a `[auto] error_retry_wait_sec` override nobody's ini has, falling back to `[collect]` and then to **0** — the probe's one miss, no wait between retries; one UNAVAILABLE event per *call* plus a `given_up` one, so a round that retried twice is three events; `attempts_used` skips executor-failed attempts that were charged; no residue discard; `evaluate()` baseline not filtered, so pure outages score 0.0; 33 tests); Ling-30-Flash — 22/26 (its `except` branch appends the feedback line and the AttemptRecord *and then* returns `unavailable=True`; an `approve()`-only fake is now unavailable via prefix; round counter left at `rnd`; tuner still counts the rows; a second config key `[auto] error_retry_wait_sec = 5`; the whole rejected-branch re-indented); Sonnet-4 (zip, no commit) — 21/26 (`OuterLoopResult` never gets `unavailable=True`, so the controller's `else` still writes knowledge.md + ticket; `_sleep(0.0)` "real wait injected by tests"; round not restored; tuner not touched); Step3-7-Flash — 20/26 (same missing `OuterLoopResult.unavailable` → exhaustion path runs; round not restored; filters `summarize_failures` in `metrics_collector.py`, which the tuner does not score from, and gives `RunRecord` defaults it never had); Dots3 — 0/26: implemented a different ticket (AUTO-BUG-21, a `[validator_agent] gate2_error_retry_wait_sec` for the per-request retry) on a premise that is false — the validator never read `[collect] error_retry_wait_sec`. Taken beyond the winner: the wait is `[collect] error_retry_wait_sec` (60 s) through a guarded `validator_unavailable_wait_sec`; the round counter is restored from the value persisted before the bump; the controller discards the unreviewed coder residue (`_discard_exhausted_residue(reason="validator unavailable")`) so it is not swept into the next task's commit — no entrant did; `OuterLoopResult.summary()` says *LEFT TODO*; `scoreable_records` on the `evaluate()` baseline so a batch of pure outages is *no baseline runs*, not a 0.0 score; 32 tests in `tests/test_gate2_validator_unavailable.py` (26 fail on the base; the last five — `approve_verdict` signature detection, the projection path, the three snapshot columns — came in with Atria Dawn's entry). `agents_128k.ini` gains `validator_unavailable_retries = 2`. Competition closed.  
**Severity:** HIGH  
**File:** `tools/auto/inner_loop.py`  
**Symbol:** `LLMGate2Validator.approve` (≈ 586–820, the `except Exception` exit that returns `(False, "validator unavailable: …")`) and the validator branch of `InnerLoop.run_task` (≈ 1708–1752, `if not approved:`)  
**Round:** 35  
**Size:** S  
**Source:** live run `baa9da87a2ab` on `../testtext2` (2026-09-16, `[critic_llm]` pointed at `ollama.com`, whose monthly quota was exhausted): task AUTO-T6 — attempts 1, 4, 5 of round 1 and 3, 5 of round 2 were *"validator rejected / validator unavailable: HTTP 429 … you have reached your monthly usage limit"*. Each cost ≈ 13 min of coder + executor work, the round feedback file told the next round the reviewer had *rejected* it, the auto-tuner scored the validator prompt on 5 "rejections" no model ever made, and the task went BLOCKED with a knowledge file and an investigation ticket — for code that was never reviewed.  
**Depends on:** —  
**Also touches:** `tools/auto/outer_loop.py` (`_write_round_feedback` ≈ 691, the BLOCKED exit ≈ 566), `tools/auto/auto_metrics.py` (`validator_status` ≈ 298), `agents.ini` (one new `[auto]` key), `scripts/trace_round_snapshot.py`, `tests/test_inner_loop*.py`

---

## What happens today

`LLMGate2Validator.approve` is fail-closed by design: any transport or
parse error becomes `(False, "validator unavailable: <exc>")`. That was
the right default when the alternative was silently approving. But the
inner loop cannot tell this `False` from a real `{"approved": false}`:

- the attempt is consumed (`records.append(AttemptRecord(attempt, True, True, False, fb))`),
- `feedback` gets `attempt N: validator rejected\nvalidator unavailable: …`,
  which the next attempt's coder prompt and the next round's
  `feedback_round_N.md` present as a reviewer verdict,
- `_prior_validator_critique` (AUTO-CR-30) is set to the error text and is
  handed back to the validator as *its own* prior critique on the next call,
- `AutoMetricsStream` records `validator_status = "rejected"`, so the
  auto-tuner promotes/demotes validator prompts on provider outages,
- five in a row exhaust the round; `max_rounds_per_task` rounds of it
  exhaust the task → BLOCKED, knowledge.md, TICKET-AUTO-Tn.

RUN-5 fixed exactly this shape for Gate 1 (*a technical failure in the
presence check is not a rejection*). Gate 2 still has it.

`tools/actions.py:526` (the interactive edit validator) already marks the
same condition with `"_api_error": True` — the precedent exists, the auto
path never adopted it.

## What must change

1. **A third outcome for Gate 2.** `Gate2Verdict` gains `unavailable: bool`
   (default `False`); `LLMGate2Validator.approve_verdict` sets it on the
   `except` exit. Keep `approve()`'s two-tuple for its many callers — the
   inner loop switches to `approve_verdict` where it exists (the AUTO-CR-30
   probe already detects the validator's capabilities; a fake validator with
   only `approve()` keeps today's behaviour). `unavailable` is set only
   for transport/parse errors — a model that answered `approved: false` is
   still a rejection, however terse.

2. **The attempt is not charged.** On `unavailable`, the inner loop
   re-runs *only the validator call* (the coder output and the executor
   result are still valid) up to `[auto] validator_unavailable_retries`
   (default `2`) with `error_retry_wait_sec` between calls. If still
   unavailable: the attempt loop ends with a new `InnerLoopResult` state —
   `passed=False`, `unavailable=True`, `attempts_used` = attempts that
   reached a verdict — and **no feedback line** is appended (the coder must
   not be told the reviewer rejected it).

3. **The task is not blocked.** The outer loop, on `unavailable`, writes
   no `feedback_round_N.md`, burns no impl version, does not call the
   rewriter, and returns the task to `todo` (state `STATUS_TODO`, with the
   round counter *not* advanced) so the next session picks it up when the
   provider is back. Log line at WARNING:
   `OuterLoop: task %s left todo — validator unavailable (%s)`. No
   knowledge file, no investigation ticket.

4. **Count it.** `AutoMetricsStream` records `validator_status =
   "unavailable"` (not `"rejected"`); the auto-tuner ignores that status
   when scoring. Trace: the gate2 stage event carries
   `status="UNAVAILABLE"` (next to `REJECTED`/`ERROR`), and
   `scripts/trace_round_snapshot.py` shows it as its own column.

5. **`_prior_validator_critique` is untouched** by an unavailable call.

## Acceptance

- [ ] `tests/`: validator stub raises / returns `validator unavailable: …`
      on every call → task leaves `run_task` with `unavailable=True`,
      `attempts_used == 0`, no `attempt N: validator rejected` line in the
      feedback, coder called exactly once (not `max_attempts` times).
- [ ] Stub unavailable twice, then `approved` → task passes on attempt 1;
      the retries are visible as three gate2 llm calls, one attempt.
- [ ] Stub unavailable twice, then `{"approved": false, …}` → a real
      rejection on attempt 1, feedback line present, attempt 2 runs.
- [ ] Outer loop: `unavailable` result → task status `todo`, no
      `feedback_round_1.md`, no `knowledge.md`, no ticket, round counter
      unchanged; the same task is offered again by the next `run`.
- [ ] `metrics.json` row says `validator_status: unavailable`; the tuner's
      score is unchanged by it.
- [ ] `_prior_validator_critique` after an unavailable call equals what it
      was before.
- [ ] `python3 -m pytest tests -q --timeout=180 && python3 -m pytest tests_bugfix -q --timeout=180` green (run sequentially).

## Out of scope

- The coder's own transport failures (RUN-8, the next ticket).
- Choosing a fallback validator provider — the operator's config; this
  ticket only stops an outage from being recorded as a review.
- The creative-mode soft-verdict path's fail-open (AUTO-CR-31) is unchanged.

## Ground rules (same as every round)

- Do not touch `CollectBridge._shrink`.
- Do not run anything against `agents_128k.ini` or any live provider.
- Do not edit `epic-tasks/`.
- One commit, no push.
