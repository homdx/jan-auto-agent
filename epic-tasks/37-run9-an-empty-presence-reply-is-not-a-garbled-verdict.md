# RUN-9 — An empty presence reply is not a garbled verdict

**Status:** landed — `4ee7a28` (base `da1e9b3`; the ideal patch written from a 20-entry contest scored on shared test data, `contest-bench/run9/` — skeleton SenseNova-6.8 var3, donor DeepSeek-v4.1 var3; `CompletionMeta` via `request_completion(on_meta=)` / `request_completion_ex`, `stream_options.include_usage` behind a per-`(url, model)` HTTP-400 memory, transport-empty re-issued unchanged in the outer retry loop (a failure inside the retry is never a verdict), `think=off` last rung, no nudge after an empty reply, three new split counters + `empty t/x` snapshot column, cap log after the pin clamp; `[gate1] presence_empty_retries = 2`; 40 + 61 tests in `tests/test_llm_stream_completion_meta.py` / `tests/test_gate1_empty_reply.py`; bench 211/211 + bonus). **Live verification 2026-09-17/18** (`../prep-run9/BASELINE.md` §RESULT, `after-run9.json`; qwen25 `945e610`, plan phase on tree `e5d46fc`, same provider config): testtext 529 cand — conf 66 / rej 370 / **unknown 2** (was 164), empty 318 all `exhausted`, 316 re-asked, gate-1 wall 6h00 (was 5h35); testtext6 504 cand — conf 66 / rej 349 / **unknown 1** (was 122), empty 230 all `exhausted`, 229 re-asked, wall 4h15 (was 3h30). UNKNOWN 286 → 3 of 1033 candidates; the provider still answers empty in ~45 % of presence calls, every one now recovered by the re-ask; cost +20–25 % gate-1 wall. The per-empty-reply WARNING is a python-logging line (stderr), not a run.log line — capture stderr to see it. Verified.  
**Severity:** HIGH  
**File:** `tools/auto/gate1_filter.py`  
**Symbol:** `Gate1Filter._check_presence` — the `_call` closure (≈ 1569–1610) and the GATE1-LEARN-2 re-ask ladder below it (≈ 1727–1845: `_empty_pin`, the `_tried` grid, the `nudge` re-ask, the final `ending unknown` return)  
**Round:** 37  
**Size:** M  
**Source:** live runs on `../testtext` (`trace_a2670f2e69fd`) and `../testtext6`, 2026-09-16, Gate 1 presence against `sensenova-6.7-flash-lite` with `unparseable_retry_mode = fast`, `presence_unknown = keep`. `log-testtext-auto.txt`: 408 presence replies with `raw=''`, 146 candidates ended `Gate1[presence] UNKNOWN`. `log-testtext6-auto.txt`: 274 empty replies, final summary `gate1 accepted=143 rejected=254 … presence_confirmed=50 presence_fail_closed=0 presence_unknown=93 presence_reask=90` — **93 of the 143 "accepted" candidates never got a verdict** (28 % of all 337) and were kept only because `presence_unknown = keep`; almost all of them are "Add companion test …" / "Document that …" items the model rejects whenever it does answer. The empty replies arrive in 20–60 s, interleaved with `HTTP 429 Server is busy` retries — not after burning a 32k budget.  
**Depends on:** nothing (RUN-5 `d22c197` is the precedent for the shape: a technical failure in the presence check is not a verdict)  
**Also touches:** `tools/llm_stream.py` (`request_completion` OpenAI SSE branch ≈ 1455–1490 — only `delta.content` is collected; `finish_reason`, `usage`, `reasoning_content` are dropped; `build_chat_request` ≈ 894 — `think=False` on the openai branch is sent as `reasoning = {"effort": "low", "exclude": true}` and nothing checks whether the provider honoured it), `agents.ini`, `tests/test_gate1*.py`, `tests/test_llm_stream*.py`, `docs/` wherever GATE1-LEARN-2 is described

---

## What happens today

`_call` returns a bare string. When the stream carried no `delta.content`
at all, that string is `""`, `_parse_presence_response` logs `JSON decode
failed (Expecting value: line 1 column 1 (char 0)) — failing closed`, and
the ladder treats the reply exactly like a garbled one: it sends the
prompt again with the nudge *"IMPORTANT: your previous reply was not valid
JSON …"* to a model that said nothing, and — because GATE1-LEARN-2 (fast)
pins the token tier on an empty reply — every later rung collapses onto
the `(32768, 0.0)` / `(32768, 0.1)` pairs already tried, so **one** re-ask
is made out of six, and the candidate ends `UNKNOWN → KEPT`.

GATE1-LEARN-2 was written from a field report where a thinking model
answered in 6–60 s *or* returned empty after ~300 s at 32k — "an empty
reply is exhaustion, not truncation". That assumption is not checked; it
is assumed from the emptiness alone. The code cannot tell apart:

- (a) **exhaustion** — the model thought for the whole budget
  (`finish_reason = "length"`, `completion_tokens ≈ max_tokens`, possibly
  everything in `reasoning_content`); the pin is right, more budget is
  more silence;
- (b) **a degraded provider** — HTTP 200, a role-only chunk or an empty
  stream, `completion_tokens` in the single digits, 20–60 s wall clock,
  `429 Server is busy` on neighbouring calls; the pin is wrong, the
  nudge is nonsense, and the right move is to try the *same* request
  again (RUN-5 shape), not to hand the candidate an unverified pass;
- (c) **truncated reasoning** — `<think>` never closed / only
  `reasoning_content` streamed; `strip_think` reduces it to `""`.

**Thinking is already "off" — on paper.** `[gate1] think = false` is the
default and `[gate1_llm]` inherits it, so every presence call already
carries `reasoning: {"effort": "low", "exclude": true}`. That is a request,
not a guarantee: `exclude` only hides the reasoning from the reply, `low`
still thinks, and a gateway that does not know the field ignores it
silently. If `sensenova-6.7-flash-lite` streams its reasoning as
`reasoning_content` and stops at `finish_reason = "length"` with an empty
`content`, the run has no way to notice that the no-think request was
ignored — the metadata that would show it is dropped in the SSE branch.
So "turn thinking off after N failures" is not available as a rung today:
it is already off from call one, and there is nothing further down to
turn off *unless* the operator set `think = true` (then a `think=False`
re-ask is a real, cheap rung the ladder does not have).

Two smaller defects in the same loop:

- The `re-ask max_tokens=131072 capped at unparseable_max_tokens_cap=65536`
  line is logged *before* the `_empty_pin` clamp brings the value back to
  32768, so the log says 65536 and then "max_tokens=32768 already tried" in
  the next line.
- `presence_reask` is counted only when a re-ask produced a verdict; the
  93 candidates above are invisible in the summary except as
  `presence_unknown`, and nothing says *why* they are unknown (empty vs.
  garbled).

## What must change

1. **Stream metadata reaches the caller.** `request_completion` (both
   branches) collects, alongside the content: `finish_reason` (last one
   seen; Ollama `done_reason`), `usage.completion_tokens` when the
   provider sends a usage chunk (else `None`; Ollama `eval_count`), the
   number of `reasoning_content` characters seen, the count of
   content-bearing chunks, and wall-clock `elapsed` seconds. Expose them
   without breaking the `str` return used everywhere: a
   `request_completion_ex(...)` returning `(text, CompletionMeta)` with
   `request_completion` delegating to it (or an `on_meta=` callback —
   pick one, keep the existing signature working, every current caller
   unchanged). For the usage chunk to exist at all on the openai branch,
   `build_chat_request` must send `stream_options: {"include_usage": true}`
   when `stream=True` — behind the same per-`(url, model)` "this field was
   rejected with HTTP 400, stop sending it" memory that AUTO-JSONMODE-1
   uses for `response_format`, so a gateway that chokes on it costs one
   failed call per run, not every call. `completion_tokens is None` stays a
   legal value and the classification below must work without it.

2. **Classify an empty reply before the ladder runs.** In `_check_presence`
   after `_call`, when `cleaned.strip() == ""`:
   - `empty_kind = "exhausted"` if `finish_reason == "length"` or
     `completion_tokens >= 0.9 * max_tokens` or `reasoning_chars > 0`;
   - `empty_kind = "transport"` otherwise (no content chunks and no
     evidence the budget was spent — including `completion_tokens is None`
     with `finish_reason in (None, "stop")` and zero content chunks).
   The classification is logged once per empty reply with the numbers it
   was made from: `Gate1._check_presence [%s]: empty reply — kind=%s
   finish_reason=%s completion_tokens=%s reasoning_chars=%d elapsed=%.1fs`.

3. **A transport-empty is retried as a transport failure, not re-asked.**
   For `empty_kind == "transport"`: re-issue the *same* request (same
   `max_tokens`, same temperature, **no nudge**) up to
   `[gate1] presence_empty_retries` (default `2`), sleeping
   `self._llm_call_retry_wait_sec` between calls, outside the
   GATE1-LEARN-2 ladder and without adding to `_tried` or setting
   `_empty_pin`. Only if the reply is still empty after those retries does
   the candidate enter the existing ladder (with the pin — at that point
   "exhaustion" is the better guess). A non-empty reply from a transport
   retry is parsed exactly as the first call's would have been.
   `empty_kind == "exhausted"` keeps today's behaviour (pin, temperature
   sweep) unchanged, plus one new rung and one diagnostic:
   - **no-think rung** — if the call went out with `think=True` (operator
     config), the *last* rung of the exhausted ladder is one re-ask with
     `think=False` at the pinned budget and the initial temperature; it
     counts as a re-ask, is logged as `re-asking … think=off`, and its
     verdict is used like any other. When `think` is already `False`
     there is nothing to switch off and the rung is skipped (logged once
     per candidate, not per attempt).
   - **ignored no-think diagnostic** — `reasoning_chars > 0` on a call that
     went out with `think=False` means the provider ignored
     `reasoning.exclude`; log it at WARNING **once per `(url, model)` per
     run** (`provider streams reasoning_content despite think=false — the
     no-think request is not honoured by %s/%s; empty replies from this
     model are exhaustion, not transport`) and count
     `presence_nothink_ignored`. Do **not** start sending other vendor
     fields (`enable_thinking`, `chat_template_kwargs`, …) — that is the
     HTTP-400 minefield AUTO-THINKDEPTH-2 documents; a follow-up ticket
     may add a per-URL cascade for it once this counter shows it matters.

4. **Say what the candidate ended as.** New counters in the Gate 1
   summary line and `_count` (under `_counter_lock` — `presence_workers`
   is 10 in the field): `presence_empty_transport` (candidates whose first
   reply was a transport-empty), `presence_empty_exhausted`, and
   `presence_nothink_ignored`. The
   final `ending unknown` WARNING and the `Gate1[presence] UNKNOWN` line
   say `empty (transport)` / `empty (exhausted)` / `garbled` instead of the
   generic `JSON decode failed (Expecting value …)` text. The nudge is
   sent only when the previous reply was non-empty.

5. **Fix the cap log.** Compute the pin clamp first (or log the cap only
   when the capped value is the one actually about to be sent) so the log
   never shows a `max_tokens` that is not the one used.

6. **Config.** `presence_empty_retries` documented in `agents.ini` next to
   `unparseable_retry_mode`; default `2`; `0` restores today's behaviour
   exactly.

## Acceptance

- [ ] `tests/test_llm_stream*.py`: an SSE stream of `role`-only chunk + `[DONE]` yields `text == ""`, `content_chunks == 0`, `finish_reason` as sent (or `None`), `completion_tokens` from the usage chunk when present; a stream that carries only `reasoning_content` deltas yields `text == ""` and `reasoning_chars > 0`; the plain `request_completion` return value is byte-identical to before for all existing tests.
- [ ] `tests/test_gate1*.py`: provider stub answers `""` (no content chunks, `finish_reason="stop"`, `completion_tokens=0`) twice then a valid rejection → candidate REJECTED, `_call` invoked 3 times at the **same** `(max_tokens, temperature)`, no nudge text in the 2nd/3rd prompts, `presence_empty_transport == 1`, `presence_reask == 0`, `presence_unknown == 0`.
- [ ] Stub answers `""` with `finish_reason="length"`, `completion_tokens == max_tokens` → today's path: pin set, one temperature re-ask with the nudge, `presence_empty_exhausted == 1`; the existing GATE1-LEARN-2 tests are unchanged and green.
- [ ] Same stub, `[gate1] think = true` → after the temperature sweep the last re-ask goes out with `think=False` (assert on the captured payload: no `think`/`reasoning_effort`, `reasoning.exclude == true`), and a valid verdict from it ends the candidate REJECTED/CONFIRMED accordingly; with `think = false` no extra call is made.
- [ ] Stub streams only `reasoning_content` deltas + `finish_reason="length"` on a `think=False` call → `presence_nothink_ignored == 1` and the WARNING is emitted exactly once for 5 such candidates in one run.
- [ ] `stream_options.include_usage` is present in the payload when `stream=True`; a stub that answers HTTP 400 mentioning `stream_options` makes the next call to the same `(url, model)` omit it, and the presence check still completes.
- [ ] Stub answers `""` (transport) on every call → after `presence_empty_retries` retries the ladder runs as today and the candidate ends `UNKNOWN`; the WARNING says `empty (transport)`; with `presence_unknown = keep` it is KEPT, with `drop` it is dropped — unchanged policy.
- [ ] `presence_empty_retries = 0` → behaviour and log lines identical to today (except the classification line and the fixed cap log).
- [ ] The `capped at unparseable_max_tokens_cap` line is never followed by a `skipping … already tried` line quoting a different `max_tokens`.
- [ ] `python3 -m pytest tests -n 4 -q --timeout=180 && python3 -m pytest tests_bugfix -n 4 -q --timeout=180` green (run sequentially).

## Out of scope

- Changing `presence_unknown = keep` policy or what Gate 2 / Pass B do with kept-unknown candidates.
- Vendor-specific hard no-think fields (`enable_thinking`, `chat_template_kwargs`, `thinking: {type: disabled}`) — a later per-URL cascade, gated on `presence_nothink_ignored` actually firing in the field.
- The coder / validator side (RUN-7, RUN-8) — same shape, different loop.
- The Ollama NDJSON branch beyond collecting the same metadata (`done_reason`, `eval_count`).

## Ground rules (same as every round)

- Do not touch `CollectBridge._shrink`.
- Do not run anything against `agents_128k.ini` or any live provider.
- Do not edit `epic-tasks/`.
- One commit, no push.
