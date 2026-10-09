# 161 — after a 200 `PATCH /config`, a reconnect failure that is not a start escapes `set_model_limit` raw: the runner re-sends the window every turn, and the next wait sits out its silence clock

**Status:** landed
**Severity:** MEDIUM
**File:** tools/contest/backend.py
**Symbol:** KiloBackend.set_model_limit, KiloBackend._reconnect_tap
**Round:** 161
**Size:** XS
**Also touches:** tests/test_contest_kilo_tap_reconnect.py

**Depends on:** 151, 159, 160 (landed on arena).

Found while judging a late cloud Sonnet 5 entry of round 151 (`ticket-151.patch`): its `test_any_failure_after_the_patch_is_a_reconnect_failure_not_a_refusal` fails on arena's code. The test itself replaces `_reconnect_tap` (an `api` difference), so the bug was reproduced again on arena without touching `_reconnect_tap` — see below.

## The bug

`set_model_limit` sends `PATCH /config`; once it answers 200, Kilo holds the window and only this round's event stream is left to fix. Ticket 151 made that case `KiloTapReconnectError`, and `runner._push_remembered_limit` reads it as **pushed** (`pushed_limit` set, never sent again). But only `_start_tap` wraps its errors. Anything else that raises inside `_reconnect_tap` — the old tap's `stop()` or `join()`, `_close_with`, an `OSError` from the lock section — comes out of `set_model_limit` as the raw exception. The runner's generic `except Exception` then logs "the remembered window was not handed to Kilo" and leaves `pushed_limit` unset, so:

1. **the PATCH is repeated at every next turn** — and each one reloads the workspace, ends its `/event` stream and runs the whole reconnect again;
2. the window *did* go over, but the log says it did not, and the in-turn watch stays armed for a size Kilo already compacts by;
3. **the next wait sits out its silence clock.** The reconnect's first wait consumed the old tap's one `tap.closed` (`EventTap.wait` consumes every event it looks at), the old tap is still the published one, and nothing records the end again — the same trap 160 fixed for the double start failure. Measured: `backend._tap.wait(tap.closed, 5)` returns `None` after the full 5.0 s.

## How to reproduce (offline, arena at d06a219)

```python
# in tests/test_contest_kilo_tap_reconnect.py, with its _repo/_backend/PUSHED_LIMIT
def test_repro(tmp_path, monkeypatch):
    ws = _repo(tmp_path / "ws")
    with FakeKiloServer() as fake:
        backend = _backend(fake, str(ws), str(tmp_path / "out" / "events.jsonl"))
        def join_breaks(self, timeout=5.0):
            raise RuntimeError("join broke")
        monkeypatch.setattr(EventTap, "join", join_breaks)
        backend.set_model_limit("kenary", "agent-a:free", PUSHED_LIMIT)
```

Result on arena: `RuntimeError: join broke` out of `set_model_limit` (expected `KiloTapReconnectError`), with exactly one `PATCH` in `fake.calls("PATCH")`. After `monkeypatch.undo()`, `backend._tap.wait(lambda e: e["type"] == "tap.closed", 5.0)` is `None` after 5.0 s.

## What to change

1. After the PATCH answered 200, **every** failure of the reconnect leaves `set_model_limit` as `KiloTapReconnectError`, the original error chained (`from exc`) and its text in the message. A `KiloTapReconnectError` from `_start_tap` stays as it is.
2. When that failure leaves the old tap published (nothing new was swapped in), stop it best-effort and record `tap.closed` again (`_close_with`), as 160 does — the next wait on it wakes at once.
3. A failure *after* a new tap was published is not this path (the connected wait already swallows its errors); do not record a second `tap.closed` on a live tap.
4. `KiloLimitRefused` and a `KiloHttpError` from the PATCH itself are unchanged — the window did not go over there.

## Tests

1. The repro above: `KiloTapReconnectError` matching "join broke", `__cause__` a `RuntimeError`, one PATCH; afterwards the published tap's `tap.closed` wait returns in < 1 s. Fails on d06a219.
2. The same with `EventTap.stop` raising instead of `join`.
3. Through the runner (`_run_one`, a remembered size below Kilo's): a reconnect that fails this way is logged as "went to Kilo but its event stream was not reopened", and the next turn sends **no** second PATCH.
4. Every test in `tests/test_contest_kilo_tap_reconnect.py` and bench 151 stays green.

## Acceptance

```bash
python3 -m pytest tests/test_contest_kilo_tap_reconnect.py contest-bench/151/acceptance_151.py -q
```

```bash
python3 -m pytest tests -n 8 -q
```

**Landed:** the patch `161-reconnect-failure-escapes-raw.patch` as it came: `set_model_limit` keeps the old tap in hand, and any failure of `_reconnect_tap` that is not already a `KiloTapReconnectError` becomes one (`from exc`); `_give_up_stale_tap` stops the old tap best-effort and records `tap.closed` again when nothing new was published, and does nothing to a tap already swapped in. Its four tests (`join` and `stop` raising, a published tap getting no second end, the runner's log line and a single PATCH) fail on the base and pass here.
Bench 151, named in the acceptance above, was red on arena before this ticket (the same nine cases on 00286a6, with or without the patch): since 153 the backend reads the window back, and the bench pushed for `m:free`, which the fake's offer does not list, and its stub client had no `model_limits`. Fixed in the bench (the model id the fake offers, a stub that answers with the last push): 28/28.
