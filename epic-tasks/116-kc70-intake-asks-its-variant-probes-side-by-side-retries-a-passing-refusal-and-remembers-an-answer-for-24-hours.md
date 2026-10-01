# KC-70 — intake asks its variant probes side by side, retries a passing refusal, and remembers an answer for 24 hours

**Status:** landed — by hand after a live bench on round 87's 8 agents (6 models), 2026-09-27; asked 2026-09-27 reading round 87's start: 80 s between the plan and the first `PROMPTED`, 70 s of it intake's probes
**Severity:** MEDIUM
**Round:** 116
**Size:** S
**File:** `tools/contest/probe_memory.py` (new), `tools/contest/variant.py`, `tools/contest/cli.py`, `tools/contest/roster.py`, `contest.ini`
**Depends on:** KC-61 (landed: a named variant is probed at intake), KC-49 (landed: `highest`), KC-11 (landed: `contest-probe.json` for `highest`), KC-67 (landed: the memory-file shape this one copies).

Ground rules, as in every ticket: `CollectBridge._shrink` stays byte-identical, and nothing runs against a live provider config.

## Problem

Round 87 was started with `--variant high` on 8 agents, 6 distinct models (two
sensenova, four kenary). Its log printed the plan at 07:47:24 and the first
`PROMPTED` at 07:48:44. The file times split the 80 s:

| Step | Time |
|---|---|
| intake — `resolve_variants`, six `say: hello` probes | ~70 s |
| `prepare_round` — 8 local clones in `../rounds/87-*` | ~8 s (07:48:34 → 07:48:41) |
| the round's `kilo serve` up and ready | ~3 s (07:48:41 → 07:48:44) |

Profiling intake alone on the same roster: 111 s, of which 105 s in
`resolve_variants` → `_probe_answer` → `hello_probe`. Each probe opens a Kilo
session and asks one question; the six were asked **one after the other**,
~17 s each on the free and flash models.

Three things made it worse than it has to be:

1. **Serial.** KC-49/KC-61 walk the roster in a loop. Six independent
   questions to six models wait for each other.
2. **Nothing remembered for a named variant.** KC-11's `contest-probe.json`
   caches `highest` only. A named variant (`high`, `max`, `xhigh`, or the
   non-standard tags some models list) is asked again on every intake — a
   `--fresh` of the same roster a minute later asks the same six again.
3. **One ask.** A timeout, an empty reply or a 429 on the single ask is the
   answer: for a named variant a note, for a `highest` rung a step down the
   ladder that KC-11 then caches for 7 days.

The probe itself stays: a named variant must still be proven at intake
(KC-61), because a dry key or a model that refuses `max`/`xhigh` has to be
seen before the round, not in it.

## Acceptance

- [x] The probes `resolve_variants` asks run in a pool of `probe_parallel`
      (8), at most `probe_per_provider` (3) against one provider at once — the
      free tiers count requests per key, and the round's agents use the same
      keys a minute later. A `highest` ladder stays one job (its rungs go top
      down, each only after the one above refused).
- [x] The agents, failures and notes are the same as the one-at-a-time walk,
      line for line, in roster order; `probe_parallel = 1` is that walk.
- [x] A rung refused for a passing reason — a timeout, an empty reply, a
      closed stream, a 429 / rate limit, a 5xx, "temporarily unavailable", a
      connection error — is asked again, up to `probe_retries` (2) more times,
      after `probe_retry_wait_sec` × n (15 s, 30 s), twice that after a rate
      limit. A quota (`quota_patterns`, or Kilo's `ProviderQuota`), a refusal
      for credentials and a refusal of the request itself (KC-45 §2's
      "provider rejected the request", a Kilo 4xx other than 408/429) are not
      asked again. This applies to a named variant and to every `highest` rung.
- [x] A named variant that answered is written to `probe-memory.json`
      (`<out_dir>/probe-memory.json`, or `probe_memory_file`) with the time it
      answered. The next intake within `probe_memory_hours` (24) asks it
      nothing; a hit does not re-stamp the record, so a success is re-proven at
      least once a day.
- [x] Eviction, on every intake that reads the offer: records past the age cut
      leave the file; so do records stamped more than 5 minutes in the future
      (a clock set back), entries that are not records, and older duplicates of
      a key. A key a live probe just saw fail is dropped too, so a success never
      outlives the probe that contradicts it. Only successes are remembered.
- [x] Silent: a probe that answers, a memory hit and a retry print nothing
      (a retry is a `DEBUG` log line). The console lines are KC-61's, unchanged.
- [x] `--reprobe` asks every named variant anyway and still writes what it saw;
      `probe_memory_hours = 0` is no memory and no file.
- [x] Fail-open: a missing, unreadable or malformed file is no memory, a write
      that cannot be made is a warning — never a refused round.

## What landed

- `tools/contest/probe_memory.py` — `ProbeRecord`, `load` (age cut, future
  cut, one record per key), `answered`, `update` (successes in, failures out,
  eviction, atomic write, no write when nothing changed, no empty file),
  `memory_path`, `hours_of`.
- `tools/contest/variant.py` — `retryable`, `retry_wait`, `with_retries`;
  `ProbeReason`, a `str` whose `quota` says Kilo named a far-off retry (the text
  printed is unchanged).
- `tools/contest/cli.py` — `resolve_variants(..., parallel=, per_provider=)` and
  `_prefetch`; `_check_offer` wraps `hello_probe` in `with_retries`, reads the
  memory for a named variant, writes it once after the probes; the KC-11 cache
  writes are under a lock now that probes run in threads.
- `tools/contest/roster.py`, `contest.ini` — `probe_memory_file`,
  `probe_memory_hours`, `probe_parallel`, `probe_per_provider`,
  `probe_retries`, `probe_retry_wait_sec`.
- `tests/test_contest_probe_memory.py` — the memory's age cut and eviction
  (boundary at exactly 24 h, one second past, future slack, duplicates,
  failures, a full day of one record), the retry rules and waits, the pool
  (six probes that only pass side by side, the per-provider cap, the pool's own
  cap, the same answers as the walk), the roster keys, and `run` end to end on
  the fake Kilo: file created, used by the next run, evicted and rewritten.
- `tests/test_contest_cli.py` — the KC-35 registered-variants fake now answers
  the probe's hello: with an empty reply the probe is (rightly) asked again.

## Live bench, 2026-09-27

Round 87's roster (`--variant high`, 8 agents, 6 models: sensenova-6.8 and
-6.7-flash-lite, kenary agnes-2-5-flash, mimo-v2-5, hy3, agnes-2-0-flash), intake
alone on the sandbox code, the operator's roster read in place, the memory in the
sandbox. Round 87 was running on the same keys; every run that asked a model
waited 60–90 s after the one before.

| Run | Intake | Probes | Console | `probe-memory.json` |
|---|---|---|---|---|
| before (serial, no memory) | 111 s | 6, one at a time, ~17 s each | — | — |
| cold (pool 8, cap 3) | 29.8 s | 6 (1.9 – 21.4 s) | empty | created, 6 records |
| warm, 2 min later | 5.7 s | 0 | empty | unchanged, not re-stamped |
| eviction: 3 records aged to 25 h, one 40 h, one 24 h in the future, one junk entry | 12.5 s | 3 — exactly the aged ones | empty | 9 entries → 6: the aged three re-stamped, the rest of the junk gone |
| burst: `--reprobe`, no provider cap, all 6 at once | 13.0 s | 6 (5.2 – 7.9 s) | empty | 6 re-stamped |

The remaining 5–6 s of a warm intake are the throwaway `kilo serve` and the gate's
own probe. Six probes at once drew no 429, so the rate limit was then provoked
the way four rounds starting together would: **four intakes at once**, each
`--reprobe` with the default caps (24 probes, 12 of them on the kenary key),
while round 87 ran on the same keys.

| Run | Intakes | Retries | What the probes met | Outcome |
|---|---|---|---|---|
| 4 × intake, 1st | 113 / 112 / 116 / 115 s | 6 (1/1/3/1) | 5 × `no answer (timeout)`, 1 × `Failed to execute statement` (Kilo's store, KC-62) | every retry answered; 4 × 8 agents at `high` |
| 4 × intake, 2nd, probe events kept | 73 / 132 / 191 / 190 s | 8 (0/2/3/3) | 20 × Kilo `session.status retry` "free-model rate limit reached; slow down and retry" (kenary's 429), 7 × `MessageAbortedError` — the probe's 60 s ran out while Kilo was still retrying | every retry answered; 4 × 8 agents at `high` |

So a free tier's 429 reaches the probe as Kilo's own retry, silently, and ends
as `no answer (timeout)` after `HELLO_TIMEOUT_SEC`. Without KC-70 those 14
probes were 14 notes (`the probe refused it`) — for `highest`, 14 rungs given
up and a lower variant cached for 7 days. With it, the next ask after 15 s
answered every time.

## Out of scope

- `prepare_round`'s ~8 s of clones and the round server's ~3 s.
- Caching a refusal or a quota: both are asked again next time.
- `highest`'s KC-11 cache (7 days, `contest-probe.json`) is unchanged; its
  rungs only gain the retry and the pool.
