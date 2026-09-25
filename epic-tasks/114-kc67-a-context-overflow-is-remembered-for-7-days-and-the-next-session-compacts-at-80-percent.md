# KC-67 — a context overflow is remembered for 7 days, and the next session compacts at 80 % of it

**Status:** queued — asked by the operator 2026-09-25 after round 113.
**Severity:** HIGH
**Round:** 114
**Size:** S
**Extends:** KC-10 (queued: compact at 80 % of `limit.context`). KC-10 relies on a limit that Kilo knows. This ticket covers the models where Kilo does not know it.

Ground rules, as in every ticket: `CollectBridge._shrink` stays byte-identical, and nothing runs against a live provider config.

## Problem

Kilo compacts a session on its own when it knows the model's context size. For many models it does not: the provider does not report a limit, and the Kilo config declares none. Such a session grows until the provider rejects it with a context overflow. With a clean tree, KC-54 then ends the agent as `STALLED`.

The provider's answer does tell us the size. Sometimes it names the limit, as in "maximum context length is 262144". Otherwise the last reply that still went through gives a size known to fit. Today this is thrown away, so the same model overflows the same way in the next round.

## Evidence (all in `contest-out/*/<agent>/events.jsonl` and `state.json`)

**Kilo compacted on its own** (`session.compacted` events) only for `sensenova-6.8-flash-lite`:

| Round | Compactions |
|---|---|
| 74 | 1 and 2 |
| 112 | 3 and 4 |
| 113 | 1 and 1 |

In round 113 (second machine) intake read `limit.context = 262144` for that model (`context_limit` in `state.json`), and Kilo compacted at 116 700 and 137 554. It did not overflow there. Where Kilo does not know the limit, it overflows like the rest: 12 times on the first machine (table below). None of the models below was compacted before it overflowed.

**Context overflows, 15 in 6 rounds** (`session.error` `ContextOverflowError`). "Last OK" is the context of the last reply that went through, meaning input + cache read + output + reasoning.

| Round | Agent (provider/model) | Provider's words | Last OK | Ended |
|---|---|---|---|---|
| 66 | laguna-s-2-1 (kenary) | exceeds the maximum context length | 259 882 | ERROR (before KC-54) |
| 74 | sensenova-6-7 var1 (sensenova 6.7-flash-lite) | limit 262 144, prompt 262 514 + 32 000 out | 262 144 | ERROR |
| 74 | sensenova-6-7 var2 | limit 262 144, prompt 262 186 + 32 000 out | 248 421 | ERROR |
| 74 | step-3-7-flash (kenary) | exceeds the maximum context length | 221 584 | ERROR |
| 112 | sensenova-6-7 var1 | limit 262 144, prompt 262 297 / 262 297 / 262 700 | 262 144 / 262 144 / 259 357 | STALLED, 3 times across the resumes |
| 112 | sensenova-6-7 var2 | limit 262 144, prompt 263 387 / 262 297 / 262 297 | 261 943 / 262 144 / 262 144 | STALLED, 3 times |
| 112 | step-3-7-flash | exceeds the maximum context length | 230 431 | revived, then READY in 113 |
| 113 | sensenova-6-7 var1 | limit 262 144, prompt 262 336 | 262 144 | STALLED, no uncommitted work |
| 113 | sensenova-6-7 var2 | limit 262 144, prompt 262 336 | 262 144 | STALLED, no uncommitted work |
| 113 | hy3 (kenary) | exceeds the maximum context length | 111 119 | STALLED, no uncommitted work |
| 113 | step-3-7-flash | exceeds the maximum context length | 232 144 | READY after a fresh session |

What the table shows:
- The same model overflows round after round at the same size. sensenova-6.7 does it at exactly 262 144 in every round. Compacting at 80 % of a remembered size (about 210 k for sensenova-6.7) would have avoided each of these.
- Some providers do not name the limit (kenary). "Last OK" is then the best number available. hy3 overflowed at only ~111 k, well below the others.
- The KC-54 ticket names 5 of 12 agents overflowing in round 91. That round is on the first machine; its five rows are in the table below.

**First machine** (`qwen25/contest-out`, `qwen26/contest-out`): 28 more overflows in 8 rounds, all
`session.error` `ContextOverflowError`. Kilo never compacted a session there: none of these
rounds had a `context_limit` for any model. "Last OK" is as above. "Prompt" is the size the
provider names, where it names one. "Agent's end" is the agent's final state, not always caused by
the overflow.

| Round | Agent (provider/model) | Provider's words | Prompt | Last OK | Agent's end |
|---|---|---|---|---|---|
| 74 | glm-4-7-flash (kenary) | exceeds the maximum context length | — | 115 610 | ERROR (before KC-54) |
| 74 | nex-n2-5-pro (kenary) | exceeds the maximum context length | — | 229 275 | ERROR |
| 75 | sensenova-6-7 var1 (sensenova 6.7-flash-lite) | limit 262 144 + 32 000 out | 263 442 | 260 860 | STALLED, no idle after 3600 s |
| 75 | sensenova-6-7 var2 | limit 262 144 | 262 381 | 262 144 | READY |
| 75 | sensenova-6-8 var1 (sensenova 6.8-flash-lite) | limit 262 144 | 262 351 | 262 056 | STALLED, no idle after 3600 s |
| 75 | sensenova-6-8 var2 | limit 262 144 | 262 381 | 262 144 | GAVE_UP |
| 91 | glm-4-7-flash (kenary) | exceeds the maximum context length | — | 116 720 | ERROR |
| 91 | hy3 (kenary) | exceeds the maximum context length | — | 104 065 | ERROR |
| 91 | laguna-s-2-1 (kenary) | exceeds the maximum context length | — | 262 112 | ERROR |
| 91 | nex-n2-5-pro (kenary) | exceeds the maximum context length | — | 222 644 | ERROR |
| 91 | step-3-7-flash (kenary) | exceeds the maximum context length | — | 150 675 | ERROR |
| 92 | hy3 | exceeds the maximum context length | — | 143 347 | READY |
| 92 | sensenova-6-7 var1 | limit 262 144 | 262 311 | 262 144 | READY |
| 92 | sensenova-6-8 var1 | limit 262 144 | 266 315 | 261 788 | READY |
| 92 | sensenova-6-8 var2 | limit 262 144, 3 times | 262 311 / 262 864 / 262 176 | 262 144 ×3 | READY |
| 99 | hy3 | exceeds the maximum context length | — | 149 359 | ERROR |
| 99 | laguna-s-2-1 | exceeds the maximum context length | — | 262 112 | ERROR |
| 99 | sensenova-6-7 var2 | limit 262 144 | 262 538 | 262 144 | ERROR |
| 99 | sensenova-6-8 var1 | limit 262 144 | 262 369 | 262 144 | ERROR |
| 99 | sensenova-6-8 var2 | limit 262 144 | 262 379 | 262 144 | ERROR |
| 99 | step-3-7-flash | exceeds the maximum context length | — | 228 779 | ERROR |
| 106 | sensenova-6-7 var2 | limit 262 144 | 262 313 | 262 144 | READY |
| 106 | sensenova-6-8 var2 | limit 262 144 | 262 279 | 262 011 | READY |
| 108 | sensenova-6-8 var1 | limit 262 144 | 262 313 | 262 144 | READY |
| 108 | sensenova-6-8 var2 | limit 262 144 | 262 313 | 262 144 | READY |
| 111 | sensenova-6-8 var1 | limit 262 144 | 262 350 | 262 144 | STALLED, no uncommitted work |

What this adds:
- sensenova-6.8 behaves like sensenova-6.7: when Kilo does not know the limit, it overflows at 262 144.
  The sensenova message is always `This model's maximum context length is 262144 tokens. However, you
  requested 32000 output tokens and your prompt contains at least N input tokens`, so the named limit
  and the prompt can both be parsed from it.
- For kenary the last OK size varies a lot for the same model: hy3 104 065 – 149 359 (and 111 119 in
  round 113), step-3-7-flash 150 675 – 232 144, glm-4-7-flash 115 610 – 116 720, nex-n2-5-pro 222 644 –
  229 275, laguna-s-2-1 262 112. A single step can add tens of thousands of tokens, so the last OK is
  only a lower bound. The smallest remembered size is the safe choice. It compacts earlier than
  needed, never later.
- Not counted: kenary's `UnknownError` "the model's provider rejected the request. check the model id,
  request fields, and context length" (52-run2 laguna 15 977, 64 agnes-3-0-flash 85 962, 64 laguna
  90 338, 74 laguna 98 499, 86-run1 nex-n2-5-pro). The sizes show it is not an overflow, so it is not
  remembered.

How the tables were built: one pass over `contest-out/*/*/events.jsonl` in both checkouts. Each
`session.error` whose name is `ContextOverflowError` is counted, filtered to the agent's own sessions
(the `session_id`s in `state.json`). Last OK is the agent's last `message.updated` for that session
without an error.

## What to do

1. **Remember.** On every context overflow, the runner adds one record to a JSON file shared by all rounds. The record holds:
   - the time, round and agent;
   - the provider and model;
   - the limit, if the provider's message names it;
   - "last OK";
   - the requested prompt size, if named.

   The file's path comes from config, with a default next to the rounds' output. The file is written atomically.

2. **Keep 7 days.** Every write drops records older than `context_memory_days` (config, default 7), like the probe cache TTL of KC-11. Records past that age are also ignored on read. A missing or broken file means no memory, never a failed round.

3. **Use it.** Before each prompt that goes into an existing session (rework, continue), find the model's size:
   1. Kilo's own `limit.context`, if it has one. The runner then does nothing more, because Kilo compacts on its own.
   2. Otherwise the smallest remembered size for this provider and model: the named limit when there is one, else "last OK".

   If the session's current context is at or above `compact_at_percent` of that size (KC-10's key, default 80), compact it first (`POST /session/{id}/summarize`, as in KC-10), then send the prompt. With no known size, do what happens today.

4. **Show it.** Each turn record gets the size used, where it came from (kilo / remembered / none), the fill % and whether it compacted. The round's start plan prints one line for each model with a remembered size.

KC-54 stays as is: an overflow that still happens still ends the same way. The only difference is that it is now recorded, so the next session of that model compacts in time.

## Acceptance

- [ ] An overflow with a named limit and one without both land in the file with the right fields. A record older than 7 days is dropped on the next write, and a broken file is treated as empty.
- [ ] A fake session at 81 % of a remembered size compacts before the rework prompt: `summarize`, then the prompt, in the request log. At 79 % it does not. When Kilo reports its own limit, the runner does not compact.
- [ ] Tests use the fake Kilo only, with no live provider and no real memory file outside `tmp_path`.
- [ ] `python3 -m pytest tests -n 4 -q` and `python3 -m pytest tests_bugfix -n 4 -q` are green.

## Out of scope

- Counting tokens ourselves: the server's numbers and the provider's words are the only sources.
- Changing KC-54's overflow handling, and compacting in the middle of a turn.
- Writing limits into the Kilo config.
