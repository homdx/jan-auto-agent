# KC-73 — a compact never blocks the permission it waits on, and an overflow Kilo already compacts is not compacted twice

**Status:** landed — by hand, reproduced offline on the fake (round 78's event order) before and after, 2026-09-27; found live 2026-09-27 in round 78 (KC-39): `agnes-2-0-flash` lost 15 of its 90 minutes in `recover_overflow`, `compact_sec = 900.1`, a permission unanswered the whole time
**Severity:** HIGH (any model that overflows loses `COMPACT_TIMEOUT_SEC` = 15 min — a sixth of `agent_max_sec` — and the old session is left running with an open ask)
**Round:** 119
**Size:** S
**File:** `tools/contest/runner.py`
**Symbol:** `settle_overflow`, `retire_session` (new, `run_agent`), `OVERFLOW_SETTLE_SEC` (new), `_wait_turn(quiet_after=)`, `KiloClient.wait_idle(quiet_after=)`, `IdleResult.compacted`, `swap_session`, `fresh_session`, `recover_overflow`, `tests/_kilo_fake.py`
**Depends on:** KC-67 (landed `dec2bb0` — `_compact_session`), KC-69 (landed — `recover_overflow`, `swap_session`, `COMPACT_TIMEOUT_SEC`, `auto: false`), KC-71 (landed — a wait answers every ask of the agent's sessions).

Ground rules, as in every ticket: `CollectBridge._shrink` stays byte-identical, and nothing runs against a live provider config.

## Problem

`_compact_session` (`runner.py:2318`) does two things **one after the other**:

1. `compact(session)` — `KiloClient.compact` (`kilo_client.py:1293`), a
   blocking `POST /session/{id}/summarize` with `timeout=COMPACT_TIMEOUT_SEC`
   (900 s, `kilo_client.py:694`);
2. only then `_wait_turn(..., on_permission=..., on_question=...)` — the one
   place that reads the tap and **answers** a `permission.asked`.

While (1) is in flight nothing answers an ask. KC-69 closed one way into that
hole: the runner's own summarize is `auto: false`, so the agent loop does not
continue after the summary. Round 78 found a second way: **Kilo's own overflow
compact.** On a `ContextOverflowError` Kilo 7.6.2 compacts the session by
itself and then **continues the agent loop** (its own compact is `auto`). The
runner reads the same `session.error` as "the turn ended with an overflow",
calls `recover_overflow` → `_compact_session`, and POSTs `summarize` into a
session that is still busy. Kilo answers that POST only when the session is
free; the session is waiting on a permission; the permission is waiting on the
runner; the runner is inside the POST. Deadlock until the 900 s timeout.

### Round 78, `agnes-2-0-flash` (`contest-out/78/`)

Session `ses_f1bb72439ffeLDyzLnrHJkzYrq`, times MSK (log in UTC, +3 h):

| Time | Source | Event |
|---|---|---|
| 22:15:51 | kilo-serve.log | `stream error … the request exceeds the model's maximum context length` (`agent=code`, step 8) |
| 22:15:52 | events.jsonl | `session.error` `ContextOverflowError` → the runner: `idle_status: "error"`, `idle_at` 22:15:52, calls `recover_overflow` |
| 22:15:52 | kilo-serve.log | step 9: **Kilo's own** compaction starts (`agent=compaction`, three streams at once); `session.status` stays `busy` — no `session.idle` ever comes |
| 22:16:09, 22:17:39 | both | the compaction hits `free-model rate limit reached`, `session.status` `retry` |
| 22:18:08, 22:18:23 | events.jsonl | two `session.compacted` |
| 22:18:23–28 | kilo-serve.log | steps 11–12, `agent=code` — **Kilo goes on with the agent loop** after its compact |
| 22:18:37 | both | `permission.asked` `per_0e44e1351001wEPEj1advnommO`, `external_directory` `/home/renat/Project/opensource/github/agent-offline/*` (`cd .. && git log --oneline -10`) |
| 22:18:37 → | — | **no reply**: no `permission.replied`, no `decisions.jsonl` for the agent, only `server.heartbeat` |
| ~22:30:52 | turns.jsonl | the POST times out: `compact_sec: 900.1`, `overflow_compacted: false` → `swap_session` → `new_session: ses_f1ba6b4c3ffe…` |

`turns.jsonl`, the turn as recorded:

```json
{"kind": "initial", "idle_status": "error", "overflow_compacted": false,
 "context_before": 0, "context_after": null, "compact_sec": 900.1,
 "swapped_from": "ses_f1bb72439ffeLDyzLnrHJkzYrq",
 "new_session": "ses_f1ba6b4c3ffevg4PSzBfqw3yTE"}
```

Three things went wrong, in order of cost:

1. **15 minutes lost.** The runner waited the whole `COMPACT_TIMEOUT_SEC` for
   a compact that Kilo had *already done* at 22:18:08 (2 min 16 s after the
   overflow), and that could not answer anyway.
2. **The ask was never answered.** Kilo's continued loop was real work (step
   11–12) — had the ask been answered, the agent would have gone on in the same,
   already compacted session, with no swap and no lost context.
3. **The old session is left running.** `swap_session` "leaves the old session
   as it is": a Kilo loop parked on an open `permission.asked`, in the same
   worktree the new session now edits. Nothing aborts it; if anything ever
   answered that ask, two sessions would write the same tree.

`context_before: 0` is a side note: `_context_tokens` read 0 on a session
whose last assistant message was the failed one — the log line says
"0 -> ?" and the number helps nobody.

## Why the fix is here, not in the gate or in Kilo

The gate works (other agents' `decisions.jsonl` fill in the same minutes); the
ask never reached it. `on_permission` is only called from `_wait_turn`, and
`_wait_turn` does not run while `compact()` blocks. Kilo's behaviour — its own
compact on overflow, then the loop goes on — is the provider's design (KC-67
already calls it "the summary Kilo starts on it", `be56cd0`); the runner must
live with it.

## What landed

The fix keys on Kilo's events only — `session.status`, `session.compacted`,
`permission.asked`, `session.idle` — never on a model's name: any model whose
overflow Kilo compacts and goes on with takes the same path.

1. **The session is waited out before anything is compacted**
   (`settle_overflow`, called at the top of the overflow edge, before the tree
   is read — Kilo's continued loop writes to that tree). `_wait_turn` with the
   turn's own budgets and silence clock, asks answered through the same
   `on_permission`, plus `quiet_after = OVERFLOW_SETTLE_SEC` (3 s):
   - nothing from the session in 3 s → `quiet`, nothing is running: the
     recovery as before (the runner's compact, the swap, KC-54's fresh session);
   - Kilo compacted it (`session.compacted` read on the way), it went idle, and
     it holds less than `compact_at_percent` (or its size is not known) → the
     work goes on in the same session with `OVERFLOW_CONTINUE`, one continue
     spent, `compacted_by: "kilo"`, no `summarize`, no swap;
   - anything else (idle without a compact, an error, a timeout, still too full)
     → the recovery as before, now on a session that is free, so the runner's
     `summarize` (`auto: false`, KC-69) asks nothing and cannot hang.
   `turns.jsonl` gets `overflow_settle_sec` and `overflow_settle_status`.
2. **`KiloClient.wait_idle(quiet_after=)`** (and `KiloBackend`, the protocol,
   `OpenRouterBackend` which ignores it): every event of the session is read,
   and none in the window ends the wait as `status="quiet"` — not aborted.
   `IdleResult.compacted` is set when a `session.compacted` of the session was
   read. Omitted, the loop is byte for byte KC-64's.
3. **The old session is stopped before a swap** (`retire_session`, in
   `swap_session` and `fresh_session`): `backend.abort(old)`, fail-open,
   `old_session_aborted` in the turn. Round 78's old session was still `busy`
   (`GET /session/status`) twenty minutes after the swap.
4. `context_before` is `null`, not `0`, when the last message reports no tokens.
5. **A second overflow inside Kilo's own loop is stopped before the runner
   compacts** (follow-up, review of `3682b6a`). When the wait ends on a
   `session.error` — the loop Kilo went on with overflowed again, and Kilo
   compacts and goes on once more — the session is busy again, and the
   runner's `summarize` would be round 78's 900 s one error later. The runner
   aborts it first (`_abort_quietly`). A wait that ends in `timeout` needs
   nothing: `wait_idle` aborts the session before it returns one, so the
   runner's compact never goes into a busy session after a timeout either.
   The wait has no KC-36 churn extension (`on_deadline`): it is Kilo finishing
   what it started, bounded by `turn_timeout_sec` and by `agent_max_sec`.
6. **A stall during the wait ends the recovery.** When the agent's hard limit
   (`agent_max_sec`) or another stall fires while Kilo is still working, the
   overflow edge neither compacts nor swaps: STALLED with the stall's own reason,
   and the KC-21 harvest as for any overflow stall.

Not done, on purpose: running the `summarize` POST on a worker thread so asks
are answered while it is open (the ticket's first draft). With the session
settled first, the runner's own compact goes into a free session with
`auto: false`, and KC-69 already showed that asks nothing; a thread would add a
second path to the tap for no case left.

### Tests

`tests/_kilo_fake.py`: a turn's `autocompact` — after the `session.error`,
busy beats, `session.compacted` (or not), an ask that blocks until answered,
optional work, then idle or an error; while it runs `summarize` blocks, as
Kilo's does (`summarize_busy_timeout`, then 504).

`tests/test_contest_runner.py`, KC-73 block:
- round 78's shape → READY, the ask answered on the same session, no
  `summarize`, `OVERFLOW_CONTINUE` into the same session, `compacted_by: "kilo"`.
  With `settle_overflow` switched off the same test ends **STALLED** — the bug;
- Kilo's loop without a compact → the ask answered, then the runner's compact
  finishes at once (the session is free);
- nothing after the error → `quiet`, the recovery as before;
- a swap aborts the old session and not the new one.
- time up while Kilo still compacts → STALLED `time up` in seconds, no `summarize`, no new session.
- a second overflow in Kilo's own loop → the session aborted before the runner's `summarize`, READY in seconds; without the abort the same test waits the fake's busy timeout out.

## Out of scope

- Whether `agnes-2-0-flash` should be allowed `cd ..` (`external_directory`
  over `agent-offline/*`): the policy's answer is its own business; the point
  here is that it is *answered*.
- Provider rate limits (`free-model rate limit reached`, `rpm exhausted`) —
  Kilo retries them itself (`session.status` `retry`).
- KC-40's deliberate, diff-aware summary before a compact (row 79): a separate
  prompt on top of this path, not a change to it.
