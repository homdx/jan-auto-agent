# RUN-8 — A transport failure mid-stream is charged to the coder

**Status:** landed — the commit after RUN-7 (`97983d8`; base `97983d8`). Competition (9 entries in `run8/` plus one commit put straight on `tickets`; reviewer probe = the ACs driven through the real `Coder` + real `InnerLoop` over a stubbed `request_completion` and a patched clock, and the real `OuterLoop` over a `StateStore` — `error_kind` on transport / NO-JSON / success; every call raises (unavailable, `attempts_used == 0`, 1 + 2 calls, 2 waits, no `coder failed` line, no REJECTED, three TRANSPORT events with `max_tokens` and `budget_raised=False`); raise once then a valid reply (passes on attempt 1, both calls at the same budget); outages + a NO-JSON + a valid reply (attempt 2, one `coder failed` record); the retry budget is per attempt; NO-JSON still consumes attempts with REJECTED events and no wait; deadline credit — 8 s inside a failing call then 3 s inside a parse failure with `max_task_seconds = 10` still runs attempt 2, the same 11 s inside parsed replies stops it, also through the outer loop's `deadline=`; retries = 0; `[auto] coder_transport_retries` read by `make_inner_loop` and present in `agents.ini`; `error_retry_wait_sec` as the wait; fresh task left `todo` at round 0 with no feedback / knowledge file, `OuterLoopResult.unavailable`, the *coder transport failure* WARNING; `cod transport` column — 40 probes; then the entrant's own tests, `tests`, `tests_bugfix`, `sync_test_tiers --check`, the RUN-4 ladder file untouched): **Sensenova-6-8 — 40/40, winner and the ideal as is** (the only entry that also credits the outer loop's shared `_task_deadline`, through `InnerLoopResult.deadline_credit_s`; `unavailable_stage = "coder" | "gate2"` on the inner and outer results, so the outer loop, `summary()` and the controller name the right half; one TRANSPORT decision event per call with `error`, `calls`, `max_tokens`, `budget_raised=False`; a dup/write failure is classified `parse`; the except-site registry updated; 39 tests in `tests/test_coder_transport_failure.py`); Step3-7-Flash — 40/40, green (the outer loop picks its wording by `unavailable_reason.startswith("coder transport failure")`, a prefix match on a string; the TRANSPORT event for a retried call is emitted after the wait, before the next call; no `_task_deadline` credit); Sonnet-5 — 39/40, green (same loop shape, `unavailable_kind`; the TRANSPORT event carries `max_tokens` but not `budget_raised`; no `_task_deadline` credit; 33 tests); Sonnet-4.6 — 38/40 but committed straight on `tickets` as `42b18a9` with **6 red tests** (no smoke mirror, no except-site registry entries, no `agents.ini` key, outer loop still says *validator unavailable*) — moved to branch `run8-sonnet46-42b18a9`, not part of the ideal; HY3 (unfinished) — 39/40, green, no tests of its own, snapshot column named differently; Ling-30 var2 — 36/40 (the deadline credit does not move the deadline — AC 4 fails in all three forms; column named differently); Ling-30 var1 — 34/40, 5 red (the RUN-7 outer-loop exit broken: the task stays `in_progress` at round 1; one TRANSPORT event for all calls; registry not updated); Dots3 (unfinished) — 32/40, green; Atria Dawn (unfinished) — `error_kind` only, the probe never returns from the every-call-raises case (no upper bound on the re-runs), 3 red; GLM5-3 — the patch is the RUN-7 commit `97983d8` exported again, no RUN-8 work. Late updates after the first close: HY3 (finished) — 39/40, 1 red (its except-site registry entries do not match its own code), no `_task_deadline` credit, nothing taken; Atria Dawn v2 — 40/40 on the probe, still no tests of its own and 3 red (registry), but the only entry that also takes the dead-socket seconds back **out of RUN-2's persisted ledger** (`deadline_started_at.txt`): every other entry, the winner included, credits only the in-memory deadline, so the very task of the source run — 80 minutes on a silent socket, left todo — would resume with its whole `max_task_seconds` already consumed and stop before its first call. Taken beyond the winner: that ledger credit (`_TaskBudget.credit_s`, subtracted from the session's elapsed time in `_end_task_budget`), with two tests; and the winner's test file now stubs `time.sleep` for every test (it slept the real 60 s `error_retry_wait_sec` per retry — ~3 minutes of the full suite). 40 tests in `tests/test_coder_transport_failure.py`. `agents_128k.ini` gains `coder_transport_retries = 2`. Competition closed.  
**Severity:** HIGH  
**File:** `tools/auto/coder.py`  
**Symbol:** `Coder.generate` (≈ 615–633, the `except Exception` around `request_completion` that returns `CoderResult(error="LLM call failed: …")`) and the `if not coder_result.succeeded` branch of `InnerLoop.run_task` (`tools/auto/inner_loop.py` ≈ 1612–1622)  
**Round:** 36  
**Size:** S  
**Source:** live run `baa9da87a2ab` on `../testtext2` (2026-09-16): task AUTO-T5, attempt 5 — `coder failed — LLM call failed: TimeoutError reading stream from https://token.sensenova.ai/…: The read operation timed out (after 0 retries, tokens already emitted)`. The socket sat silent for 80 minutes (23:37 → 00:58) before the read timeout fired; that ate the task's `max_task_seconds = 3600` budget, so the outer loop stopped after **one round** and the task went BLOCKED with a knowledge file that says the coder failed. The coder had produced nothing wrong — it had produced nothing at all.  
**Depends on:** RUN-7 (the same three-outcome shape on the validator side; reuse its `InnerLoopResult.unavailable` and the outer loop's *left todo* exit)  
**Also touches:** `tools/llm_stream.py` (`request_completion` ≈ 1498–1503, the `tokens already emitted` raise), `tools/auto/inner_loop.py`, `tools/auto/outer_loop.py`, `agents.ini`, `scripts/trace_round_snapshot.py`, `tests/test_coder*.py`, `tests/test_inner_loop*.py`

---

## What happens today

`request_completion` retries a stream that dies *before* the first token,
but once tokens have been emitted it raises — correctly, a stream cannot
be resumed. `Coder.generate` catches every exception the same way:
`CoderResult(error="LLM call failed: …")`, `succeeded=False`. The inner
loop sees a failed attempt: `attempt N: coder failed — …` goes into the
feedback (the next attempt's prompt tells the model *it* failed), the
attempt is consumed, the coder stage is traced `REJECTED`, and the round
feedback / knowledge file / tuner all count it as a coder failure. The
RUN-4 ladder cannot help (no reply was parsed, so no tier is learned) and
the `max_tokens` on that decision event is the pre-raise budget, so the
snapshot's `cod esc` column reads it as a cut-off.

The time is the worse half: the wall-clock spent waiting on a dead socket
is charged to the task's `max_task_seconds` (RUN-2 only stops the clock
when the *run* is stopped), so a single hung stream can end the task.

## What must change

1. **Classify the failure.** `CoderResult` gains `error_kind: str`
   (`""`, `"parse"`, `"transport"`). `Coder.generate` sets `"transport"`
   when the `request_completion` call raised (any exception out of the
   call itself — `RuntimeError` from the stream-read path, `TimeoutError`,
   `URLError`, `HTTPError`); `"parse"` for today's NO-JSON / decode /
   cut-off paths. The `error` text is unchanged.

2. **A transport failure is not an attempt.** In `run_task`, a
   `transport` result re-runs the coder call on the *same* attempt number
   up to `[auto] coder_transport_retries` (default `2`), waiting
   `error_retry_wait_sec` between calls, without appending a feedback line
   and without touching `prior_feedback`. Traced as coder stage
   `status="TRANSPORT"` (not `REJECTED`), with the `max_tokens` the call
   went out at and `budget_raised=False`.

3. **After the retries: left todo, not blocked.** Same exit as RUN-7:
   `InnerLoopResult(passed=False, unavailable=True, …)`; the outer loop
   writes no round feedback, burns no impl version, returns the task to
   `todo` with the round counter unchanged; WARNING log
   `OuterLoop: task %s left todo — coder transport failure (%s)`.

4. **The wait is not the task's.** Time spent inside a `request_completion`
   call that ends in a transport failure is credited back to the task's
   shared deadline (`_eff_deadline` in `run_task` / `_task_deadline` in the
   outer loop) — measure around the call, add the elapsed seconds to the
   deadline on a transport result. Coder time that produced a reply
   (parsed or not) is still charged.

5. **Count it.** `scripts/trace_round_snapshot.py` shows `cod transport`
   next to `cod esc`; `AutoMetricsStream` records the task's exit as
   `validator_status = "unavailable"` (RUN-7's value — no verdict was
   reached).

## Acceptance

- [ ] `tests/`: `request_completion` patched to raise `RuntimeError("TimeoutError reading stream … tokens already emitted")` on every call → `run_task` returns `unavailable=True`, `attempts_used == 0`, coder called `1 + coder_transport_retries` times, no `coder failed` line in the feedback, no `REJECTED` coder trace event, `TRANSPORT` events present.
- [ ] Raise once, then a valid reply → task passes on attempt 1; the two coder decision events carry the same `max_tokens`.
- [ ] A NO-JSON reply is still `error_kind == "parse"`, still consumes the attempt, still climbs the RUN-4 ladder — the ladder tests in `tests/test_run4_coder_budget_ladder.py` are unchanged.
- [ ] Deadline credit: with `max_task_seconds` = 10 and a patched clock that advances 8 s inside the failing call, the next attempt is still allowed (the deadline moved by 8 s); with the same 8 s inside a *parsed* reply it is charged.
- [ ] Outer loop: `unavailable` result → task `todo`, no `feedback_round_1.md`, no `knowledge.md`, no ticket.
- [ ] `python3 -m pytest tests -n 4 -q --timeout=180 && python3 -m pytest tests_bugfix -n 4 -q --timeout=180` green (run sequentially).

## Out of scope

- Resuming a broken stream (impossible with the provider APIs in use).
- Lowering the provider `timeout` — operator config; the 80-minute silence is the provider's stream-read timeout as configured.
- The validator side (RUN-7).

## Ground rules (same as every round)

- Do not touch `CollectBridge._shrink`.
- Do not run anything against `agents_128k.ini` or any live provider.
- Do not edit `epic-tasks/`.
- One commit, no push.
