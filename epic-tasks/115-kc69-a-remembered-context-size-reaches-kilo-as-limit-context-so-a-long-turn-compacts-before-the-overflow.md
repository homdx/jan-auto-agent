# KC-69 — a remembered context size reaches Kilo as `limit.context`, so a long turn compacts before the overflow

**Status:** landed — by hand after a live bench on 8 models, 2026-09-27; found 2026-09-26 scoring round 70 on two machines
**Severity:** MEDIUM
**Round:** 115
**Size:** S
**File:** `tools/contest/cli.py`, `tools/contest/context_memory.py`, `tools/contest/runner.py`, `tools/contest/kilo_client.py`, `tools/contest/policy.py`
**Depends on:** KC-67 (landed `dec2bb0`), KC-56 (landed), KC-35 (landed).

## Problem

Round 70 read the KC-67 memory correctly on both machines. The memory is keyed
by provider and model and is shared by every round. The `round` field only says
where a record came from. On this machine sn67-var1 picked up round 68's record,
`context_source: remembered`, 262 144. Its second prompt went out at 76.9 %, just
below the 80 % mark, and it did not overflow.

On the second machine, sn67-var2 overflowed **inside its first turn**. The fill
was 0.0 at the prompt, and the provider then reported:

    maximum context length is 262144 tokens. However, you requested 32000 output
    tokens and your prompt contains 263159 input tokens

The KC-67 gate only runs **between** prompts, so it cannot stop growth inside one
long turn. Kilo cannot stop it either: sensenova declares no `limit.context`, so
Kilo does not know when to compact. KC-54 rescued the work with a fresh session
on the dirty tree, and the agent finished READY. The overflow still cost the
turn.

There are two defects:

1. The remembered size stays with the runner and never reaches Kilo. When intake
   has no `limit.context` for a model, the smallest remembered size should go
   into the spec's `context_limit` and the KC-35 overlay as `limit.context`, with
   `limit.output` when the error names one. Kilo then compacts that model
   mid-turn, the same way it does the models it knows (`context_source: kilo`,
   which is what the sn68 entries got on the second machine).
2. `size_of` returns the provider's `limit`, but the provider also reserves the
   requested output (32 000 here). The prompt budget is therefore
   `limit - output`, 230 144, not 262 144. `parse_overflow` should also read
   `requested N output tokens`, and the size should be `limit - output` when both
   are known.

Checked 2026-09-26 against Kilo 7.6.2: `kilo models sensenova123 --verbose` without
an overlay shows `limit {context: 0, output: 0}`. With
`KILO_CONFIG_CONTENT` = `{"provider":{"sensenova123":{"models":{"sensenova-6.7-flash-lite":
{"limit":{"context":230144,"output":32000}}}}}}` it shows
`limit {context: 230144, output: 32000}`. Kilo takes the key from the same overlay
KC-35 already sends.

## Acceptance

- [x] A remembered 262 144 with output 32 000 puts the model's real window into
      the overlay — `limit {context: 262144, input: 204115, output: 32000}` —
      and the budget 230 144 on `spec.context_limit`.
- [x] A model whose intake limit is known keeps intake's number.
- [x] `parse_overflow` / `parse_output` read `requested 32000 output tokens`;
      an old record without it keeps today's size.
- [x] `tests` then `tests_bugfix -n 4`, green, run sequentially.

## What landed

The live bench (below) turned the S ticket into the whole path, because each
piece the ticket asked for failed on a real model in a way no fake showed:

1. **The overlay.** `limit.context` is Kilo's hard wall, so it stays the real
   window (`size + output`); a wall cut to the threshold ended turns in
   `Compaction exhausted … after 3 attempts` (sensenova-6.7). Kilo compacts
   inside a turn when a step reaches `limit.input - reserved` (20 000), and it
   skips the preflight `compaction.threshold_percent` mid-turn (laguna: one
   compact at a turn's start, then 53 % inside it) — so `input` is
   `compact_at_percent` of the budget + 20 000. `limit.output` is always sent:
   without it 7.6.2 rejects the `limit` as `ConfigInvalidError` and drops the
   **whole** `KILO_CONFIG_CONTENT`, the KC-35 registration included (laguna).
2. **The permission gate** (the operator's plan). Every permission asked logs
   the fill — console and `decisions.jsonl` `context` — and at or past the
   threshold it is refused (layer `context`), the turn is aborted once (the
   model otherwise goes on with the tools that need no ask: glm, four refusals
   at 60 %), and the next prompt compacts. A compact that fails or leaves the
   session at or past the threshold goes on in a **new session**: the round
   prompt, then `CONTEXT_CONTINUE_NOTE` with the old session's summary.
3. **The compact itself.** `summarize` is `auto: false` — `true` runs the agent
   loop after the summary, whose next permission nothing answers while the call
   still waits (sensenova-6.7 hung). The call waits 900 s, not 30 (sensenova
   writes a summary for longer). A chunked summary has 0 tokens, so its size is
   its text / 4, and the fill after a compact is the summary, not the reply
   before it — the old read refused every later ask (laguna).
4. **A clean overflow goes on.** An overflow with nothing written and a
   continue left is compacted and continued (`OVERFLOW_CONTINUE`), or moved to a
   new session — no longer KC-54's stall. The runner's between-prompt gate works
   for every known size, not only a remembered one.

## Live bench, 2026-09-27

A sandbox whose ticket makes the agent read ~170 k tokens of our own source,
driven by the real `run_agent`, `compact_at_percent = 30`:

| model | memory at start | result | what happened |
|---|---|---|---|
| sensenova-6.7-flash-lite | 230 144 | READY 572 s | Kilo compacted mid-turn |
| sensenova-6.8-flash-lite | 230 144 | READY 346 s | Kilo compacted mid-turn |
| laguna-s-2-1:free | 215 042 | READY 1120 s | |
| glm-4-7-flash:free | none | READY 1133 s | |
| glm-4-7-flash:free | 117 797 (its own overflow) | GAVE_UP `no_progress_row` | fill ≤ 21.6 %: Kilo compacted mid-turn; the sandbox has no `append_task.py` |
| agnes-3-0-flash:free | none | READY 282 s | |
| space-bunny-alpha-bynara | none | READY 1565 s | |
| hy3:free | none | STALLED | overflow → compacted 189 145 → 12 330, went on; 2nd compact hit the free-model rate limit → new session; the ticket read the file again and overflowed a 3rd time. Size remembered for the next round |

A model the memory does not know yet can still overflow inside the round that
meets it: the overlay is built when the round's server starts.

## Out of scope

Compacting inside a turn from the runner. Once Kilo knows the size, Kilo does it.
