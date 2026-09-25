# KC-45 — "the model's provider interrupted the response stream" is a dropped connection, so it is retried like one instead of ending the agent `ERROR`

**Status:** landed `fbfa62b` (2026-09-25) — round 89 winner sensenova-6-8-flash-lite-var1. Was queued: found live 2026-09-23 in round 86 (`laguna-s-2-1:free`, then `agnes-3-0-flash:free` twelve minutes later); the same payload ended an agent in round 58 (`muse-spark-1-3`) and round 59 (`mimo-v2-5:free`).
**Severity:** MEDIUM (one slot of the round is lost to a transient provider fault KC-19 was written to absorb; three rounds out of the last dozen lost one, round 86 lost two)
**File:** `tools/contest/runner.py`
**Symbol:** `_RETRYABLE_MSG_RE`, `_retryable`
**Round:** 89
**Size:** S
**Source:** round 86, `contest-out/86/laguna-s-2-1/events.jsonl`:

```json
{"type": "session.error", "properties": {"sessionID": "ses_f31195b43ffepgGBcmy1HlZJgW",
 "error": {"name": "UnknownError",
           "data": {"message": "\"the model's provider interrupted the response stream\""}}}}
```

and `contest-out/86/kilo-serve.log` for the same session: three
`free-model rate limit reached; slow down and retry` stream errors (15:34:13,
15:35:15, 15:36:23 — Kilo retried each on its own), then at 15:37:06
`the model's provider interrupted the response stream`, which Kilo does not
retry and surfaces as the `session.error` above. The runner put the agent in
`ERROR` on the spot: one turn, `attempt 0`, clean worktree.

KC-19 made a retryable session error a re-prompt into the same session, and
`_retryable` (`runner.py:113`) decides what is retryable: `data.isRetryable`,
a socket code in `data.metadata.code`, or `_RETRYABLE_MSG_RE`
(`429|502|503|504|overloaded|rate limit|timeout`). This payload carries none
of the three — no `isRetryable`, no `metadata`, and a message that names a
dropped stream without a status code. So the one failure that *is* literally
"the provider dropped the connection mid-turn" — the words `RETRY_PROMPT`
itself uses — is classified as permanent.

The same payload, `state.json` `last_error`, `ERROR`, one turn:

| round | agent | model |
|---|---|---|
| 58 | `muse-spark-1-3` | `muse-spark-1-3` |
| 59 | `mimo-v2-5` | `mimo-v2-5:free` |
| 86 | `laguna-s-2-1` | `laguna-s-2-1:free` |
| 86 | `agnes-3-0-flash` | `agnes-3-0-flash:free` |

**Depends on:** KC-19 (`_retryable`, `RETRY_PROMPT`, `max_error_retries`, landed `b135e19`).
**Also touches:** `tests/test_contest_runner.py`

---

## What must change

### 1. Two more transient messages are retryable

`_RETRYABLE_MSG_RE` also matches, case-insensitively:

- `interrupted the response stream` — the round 58/59/86 payload;
- `upstream unavailable` — Kilo's text for the same class on the stream log
  (`upstream unavailable for model hy3:free`, round 86, 15:33:55), so the day
  it reaches `session.error` it is already covered.

Nothing else changes in `_retryable`: the backoff, `max_error_retries`, the
`RETRY_PROMPT` re-prompt into the same session and the `ERROR` after the
budget is spent are KC-19's, untouched.

### 2. What stays not retryable

`the model's provider rejected the request. check the model id, request
fields, and context length` stays **not** retryable. In round 86
`nex-n2-5-pro:free` got it on its very first request, before any tool call —
a request the provider refuses is refused again when resent. The test pins
this so a later, looser pattern (`provider`, `stream`) cannot swallow it.

KC-19's list — `Model not found`, a context-length error, an unknown shape, a
non-dict payload — is unchanged.

### 2a. …unless the same session already had answers (addendum 2026-09-24, round 74)

§2's reason holds only for the **first** call of a session: a request that is
refused before any answer will be refused again. Round 74 shows the other case.
`nemotron-3-super-120b-a12b` (`kenary`) finished five steps with
`finish: tool-calls`, input 13 481 → 25 596 tokens. Two `session.status`
`retry` events with `free-model rate limit reached; slow down and retry`
followed, at 14:14:21 and 14:15:32. Then, at 14:16:09:
`"the model's provider rejected the request. check the model id, request
fields, and context length"` → `ERROR`. The model id and the fields had just
worked five times, and 26 k is far below its context. This is the free tier
refusing under load, the same class as the 429 two lines above it. The same
payload ended `agnes-2-0-flash` (after 11 calls, 143 k) and `agnes-2-5-flash`
(after 14 calls) in round 84.

So the rule becomes: `provider rejected the request` is **not** retryable when
the session has no assistant message with a `finish`, which is §2's
first-request case, unchanged. It **is** retryable, under KC-19's same budget
and backoff, once the session has at least one finished assistant message.
`_retryable` gets the count from the caller (`run_agent` already has the
session; `KiloClient.messages`, fail-open → `0`, which keeps §2's behaviour).
The §2 pin test stays, and a second test pins the mid-session case.

### 3. `_retry_reason` names it

The `RETRY_PROMPT` reason for this payload is the message with its extra
surrounding quotes stripped (`the model's provider interrupted the response
stream`). Today `_retry_reason` returns it wrapped in the literal `"…"` Kilo
puts around it (checked on `a7bca10`).

## Out of scope

- Retrying inside Kilo. The runner re-prompts; Kilo's own stream retry is its own.
- Rate-limit pressure from `--max-parallel 12` on one free endpoint — an
  operator choice, not a classification bug.
- The gate failing under the same load — KC-37.

## Acceptance

- [ ] `_retryable({"name": "UnknownError", "data": {"message": "\"the model's provider interrupted the response stream\""}})` is `True` — the literal round 86 payload, quoting included.
- [ ] `_retryable` on `{"name": "UnknownError", "data": {"message": "upstream unavailable for model x"}}` is `True`.
- [ ] `_retryable` on the round 86 `nex-n2-5-pro` payload (`the model's provider rejected the request. check the model id, request fields, and context length`) is `False`.
- [ ] A runner test in the shape of `test_retryable_error_reprompts_same_session_and_recovers`: the first idle carries the interrupted-stream payload, the agent is re-prompted with `RETRY_PROMPT` in the **same** session and ends `READY`, not `ERROR`.
- [ ] `_retry_reason` for the interrupted-stream payload neither starts nor ends with `"`.
- [ ] (§2a) The same `rejected the request` payload in a session that already has one assistant message with `finish: "tool-calls"` is retried: `RETRY_PROMPT` in the same session, with KC-19's budget. On the first call, with no finished message, it is still `ERROR`, as in the test above.
- [ ] (§2a) `messages()` raising while this is decided → treated as no finished message (not retried).
- [ ] Every existing KC-19 test passes untouched (`test_non_retryable_error_is_not_retried`, `test_max_error_retries_zero_means_no_retry`, …).
- [ ] `tests` and `tests_bugfix` green; `CollectBridge._shrink` byte-identical.
