# KC-69 — a remembered context size reaches Kilo as `limit.context`, so a long turn compacts before the overflow

**Status:** queued — found 2026-09-26 scoring round 70 on two machines
**Severity:** MEDIUM
**Round:** 115
**Size:** S
**File:** `tools/contest/cli.py` (`_with_context_limits`, the KC-35 `KILO_CONFIG_CONTENT` overlay), `tools/contest/context_memory.py` (`size_of`)
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

- [ ] A remembered 262 144 with output 32 000 for `provider/model` that intake
      has no limit for puts `limit.context = 230144` (or 262 144 with
      `limit.output = 32000`) into the overlay for that model, and `context_limit`
      on its spec.
- [ ] A model whose intake limit is known keeps intake's number. The memory never
      overrides Kilo.
- [ ] `parse_overflow` reads `requested 32000 output tokens`. An old record
      without it keeps today's size.
- [ ] `tests` then `tests_bugfix -n 4`, green, run sequentially.

## Out of scope

Compacting inside a turn from the runner. Once Kilo knows the size, Kilo does it.
