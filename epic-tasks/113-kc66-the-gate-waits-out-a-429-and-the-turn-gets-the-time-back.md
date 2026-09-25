# KC-66 — the gate waits out a 429, and the turn gets the time back

**Status:** queued — asked by the operator 2026-09-25.
**Severity:** MEDIUM
**Round:** 113
**Size:** S

Ground rules, as in every ticket: `CollectBridge._shrink` stays byte-identical, and nothing runs against a live provider config.

## Problem

The gate is the check that approves or rejects an agent's action. KC-55
already retries a gate call that gets a 429 (`tools/contest/policy.py`,
`[contest] gate_retries = 3`, `gate_retry_wait_sec = 10`, the server's
`Retry-After` up to `gate_retry_max_wait_sec = 60`), and rejects the action
only when that budget runs out. Two gaps are left:

- The budget is too short for a free gate key shared with the agents: three
  waits of 10 s, and the action is rejected while the key is still cooling.
- Every second the gate waits comes out of the agent's turn: the gate runs
  inside `KiloClient.wait_idle`'s loop, so the agent pays for the gate's 429.

## What to do

1. The gate gets a 429: wait 30 s and ask again, up to 5 tries in all. Both numbers come from config — the KC-55 keys `gate_retries` / `gate_retry_wait_sec`, new defaults in `contest.ini`, no new keys. A `Retry-After` still wins, and one past `gate_retry_max_wait_sec` is still a quota reset. Reject only if every try got a 429.
2. Count the tries and add time to the turn: 1–2 tries +1 min, 3–4 tries +2 min, and so on.
3. Record the tries and the added time in the decision log and in the turn.

## Tests

- A 429 a few times, then an answer: the action goes through, and the turn is longer by the right amount.
- A 429 every time: rejected after 5 tries, and the turn is still longer.
- No 429: no wait, no added time.
- No test waits for real.
