# RUN-8 — A transport failure mid-stream is charged to the coder

**Status:** queued — written 2026-09-16; on offer once RUN-7 (ticket 35) has landed  
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
- [ ] `python3 -m pytest tests -q --timeout=180 && python3 -m pytest tests_bugfix -q --timeout=180` green (run sequentially).

## Out of scope

- Resuming a broken stream (impossible with the provider APIs in use).
- Lowering the provider `timeout` — operator config; the 80-minute silence is the provider's stream-read timeout as configured.
- The validator side (RUN-7).

## Ground rules (same as every round)

- Do not touch `CollectBridge._shrink`.
- Do not run anything against `agents_128k.ini` or any live provider.
- Do not edit `epic-tasks/`.
- One commit, no push.
