# KC-75 — a queued nudge's turn is waited out, and an empty reply after a provider 4xx/5xx is retried on its own budget

**Status:** landed `af3b02f` — by hand, 2026-09-28; found in round 120 (KC-74), first run
**Severity:** HIGH (all 11 agents GAVE_UP in about 5 minutes)
**Round:** 122
**Size:** S
**File:** `tools/contest/runner.py`, `tools/contest/kilo_client.py`, `tools/contest/roster.py`, `contest.ini`
**Symbol:** `QUEUED_NUDGE_QUIET_SEC` (new), `IdleResult.provider_errors` (new), `ContestConfig.max_silent_retries` (new), `ContestConfig.silent_retry_max_backoff_sec` (new)
**Depends on:** KC-42 (landed `0c60459` — first-touch nudge), KC-73 (landed — `quiet_after`)

## Problem

KC-42's first-touch nudge (420 s) was posted into a busy session. Kilo 7.6.2
queued it: the running turn closed (`session.idle`), then `queue.changed` and
`turn.open` started the queued prompt. The runner took that first idle as the
nudge's own reply. From then on every continue was queued behind a running
turn, cut the model to one step and read as SILENT. On top of that, kenary's
free models were returning 429s, and those turns came back empty too.

## Fix

1. For each nudge sent, the runner waits for one more idle with a 5 s
   `quiet_after` window and no `since`. The new result replaces the first idle
   only if it ends in idle or error. `turns.jsonl` records `queued_waits`.
2. `wait_idle` counts `session.status` `retry` events as `provider_errors`.
   If a turn is SILENT and `provider_errors > 0`, the runner sends a continue
   again without spending `max_continues_per_attempt`. It does this up to
   `max_silent_retries` times (8 in `contest.ini`, 0 by default) with a backoff
   of 15 s that grows to at most 120 s (`silent_retry_max_backoff_sec`). The
   counter resets after a non-SILENT idle and after rework. `turns.jsonl`
   records `silent_retries` and `provider_errors`, and a log line reports each
   retry.

Tests: 6 in `tests/test_contest_runner.py`, including
`test_the_idle_before_a_queued_nudge_is_not_the_nudges_turn`, which is red
without the fix.
