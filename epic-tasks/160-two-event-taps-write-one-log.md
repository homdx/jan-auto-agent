# 160 — after a config reload two event taps append the same events to one `events.jsonl`

**Status:** landed
**Severity:** MEDIUM
**File:** tools/contest/backend.py
**Symbol:** KiloBackend._reconnect_tap, KiloBackend._start_tap
**Round:** 160
**Size:** S
**Also touches:** tests/test_contest_kilo_tap_reconnect.py

**Depends on:** 151.

Found by the round-151 cross-tests: cloud Sonnet 5's test `test_a_reload_that_does_not_end_the_stream_is_bounded_and_logged` failed on arena's 151 code with "the old tap was stopped, one writer on the log — `join(0)` is False". Ticket 151 item 4 named the risk; the landed code logged it and kept it.

## The bug

151's `_reconnect_tap` starts the **new** tap first and only then waits for the old stream's end, stops the old tap and joins it with what is left of the one deadline. When the reload does not end the old stream (the case 151's bound is for), that wait spends the whole deadline and the join gets `0`: the old tap is still reading when `_reconnect_tap` returns, and both taps append every event to the same `events.jsonl` — the new one from its start, the old one until its reader notices the stop. The log is read back by `runner._leg_pytest_runs` (a leg's pytest report counts every run it finds) and by the idle watch, so the doubled lines are not cosmetic.

Reproduced offline: a client whose `set_model_limit` reloads nothing, `_reconnect_tap(settle=0.5)` — the old tap's thread is alive at the moment the new tap starts, every time.

## Fix

- Old first: wait for its `tap.closed` within the deadline (logged when it does not come), `stop`, `join` for at least `_OLD_TAP_JOIN_SEC` (2 s — `stop` shuts the socket, the reader is gone well inside it; a join of zero is what left two writers), logged when it still runs. Only then start the new tap (retried once, `KiloTapReconnectError` after two failures).
- Nothing is lost by the order: after a reload Kilo has already ended the old stream, so the old tap was never a live fallback.
- Two failures: the wait above consumed the old tap's only `tap.closed`, so the failure records a fresh one (in memory — the old log is closed) before raising; the next `wait_idle` on that tap returns `closed` at once instead of sitting out its silence clock. Found by the winner's own `test_a_reconnect_that_cannot_reopen_raises_and_keeps_the_live_tap` while making this fix.

## Tests

1. `test_the_old_tap_is_gone_before_the_new_one_writes`: no reload, `settle=0.5` — the old tap's thread is dead when the new tap's `start` runs; an event sent afterwards is in `events.jsonl` exactly once. Fails on 156.
2. `test_a_reconnect_that_cannot_reopen_raises_and_keeps_the_live_tap` (updated): two failed starts — `KiloTapReconnectError`, the old tap stopped, a fresh `tap.closed` last, `wait_idle` returns `closed` within 10 s.
3. Bench 151 A1–A4 unchanged and green.

## Acceptance

```bash
python3 -m pytest tests/test_contest_kilo_tap_reconnect.py contest-bench/151/acceptance_151.py -q
```
