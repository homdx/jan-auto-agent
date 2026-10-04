# 159 — `interrupt()` (Ctrl-C) waits for the whole event-tap reconnect

**Status:** landed
**Severity:** MEDIUM
**File:** tools/contest/backend.py
**Symbol:** KiloBackend._reconnect_tap, KiloBackend.interrupt
**Round:** 159
**Size:** S
**Also touches:** tests/test_contest_kilo_tap_reconnect.py

**Depends on:** 151, 160.

Found by the round-151 cross-tests: cloud Sonnet 5's `test_an_interrupt_during_the_reconnect_stops_the_new_tap_too` on arena's 151 code — the test calls `interrupt()` while the new tap's start is held and releases it only after `interrupt()` returns; on arena that `interrupt()` did not return until the held start gave up (10 s), and the start's retry doubled it.

## The bug

Ticket 151 asked for one lock shared by `_reconnect_tap` and `interrupt()`, so that a Ctrl-C between "stop the old tap" and "publish the new one" cannot leave a tap running. The landed code held that lock across the **whole** reconnect: the wait for the old stream's end, its join, the new tap's start (retried once) and the wait for `server.connected` — up to *settle* (5 s) and the whole of a start that hangs (a slow `open`, a socket that does not answer). `interrupt()` takes the same lock, so the operator's Ctrl-C sat behind it.

Reproduced offline: the new tap's `start` held on an event for 10 s, `interrupt()` called meanwhile — it returns after 10.0 s on 160's code.

## Fix

- The lock covers reading `self._tap` and publishing the new tap, nothing else. The waits and the start run without it.
- Published into an interrupted backend: the new tap is stopped under the lock, and the reconnect returns without waiting for `server.connected`.
- Already interrupted when the reconnect begins: no new stream is opened at all; the old tap is stopped and stays published (cloud Sonnet 5 and sensenova-6.7-var2 both test this — two independent authors).
- When the reconnect returns into an interrupted backend, the new tap's reader has **exited**, not just been told to: it is joined (`_OLD_TAP_JOIN_SEC`) after its stop — sensenova-6.7-var2's race test caught it still alive right after the return.
- An interrupt that lands during the `server.connected` wait: that wait now stops on `tap.closed` as well and records it again (like 160's double failure), because `EventTap.wait` consumes every event it looks at and the caller's next `wait_idle` would otherwise sit out its silence clock — found by the winner's own `test_interrupt_racing_a_reconnect_leaves_no_tap_thread_running` while making this fix.

## Tests

1. `test_an_interrupt_during_a_slow_reconnect_returns_at_once`: the start held 10 s, `interrupt()` returns in < 0.5 s, then no tap thread is left. Fails on 160 (10.0 s).
2. `test_interrupt_racing_a_reconnect_leaves_no_tap_thread_running` (the winner's): the interrupt during the `server.connected` wait; `wait_idle` returns `closed` within 10 s.
3. `test_a_reconnect_after_an_interrupt_opens_no_stream` and `test_a_reconnect_that_publishes_into_an_interrupt_leaves_no_thread`: both fail without the entry check and the join.
4. Bench 151 A2.

## Acceptance

```bash
python3 -m pytest tests/test_contest_kilo_tap_reconnect.py contest-bench/151/acceptance_151.py -q
```
