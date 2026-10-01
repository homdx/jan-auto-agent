# KC-64 — a provider that keeps failing ends the agent after N retries, not at the turn deadline

**Status:** landed `792d6ae` (2026-09-25) — round 111, winner sensenova-6-7-flash-lite-var1 (33/33 on the round's acceptance bench; the run was cut short when the operator's console closed, 9 of 11 entries had work and were scored as they stood); found 2026-09-25 reading round 106 (KC-59): `laguna-s-2-1` and `nex-n2-5-pro` (both `bynara`) never produced one token. Kilo retried the model 51 and 50 times over 60 min, and each retry's `session.status busy` reset the runner's silence clock. So both agents sat `WAITING` until `turn_timeout_sec` and ended `STALLED: no idle after 60m (0 files, 0 lines, unchanged for 60m)`. The real reason, "The model service is temporarily unavailable", is in no field of `state.json`.
**Severity:** MEDIUM (no work is lost, since the model never answered, but the agent holds a round slot for the full hour, the round's end waits for it, and the report names the wrong cause)
**File:** `tools/contest/kilo_client.py`, `tools/contest/backend.py`, `tools/contest/runner.py`, `tools/contest/roster.py`, `contest.ini`, `tests/_kilo_fake.py`
**Symbol:** `KiloClient.wait_idle`, `IdleResult`, the runner's `idle.status == "error"` block, `ContestConfig`
**Round:** 111
**Size:** S
**Source:** round 106, `contest-out/106/laguna-s-2-1/events.jsonl`, `contest-out/106/nex-n2-5-pro/events.jsonl`, `contest-out/106/state.json`. The times are seconds after the round started at 08:30:21 UTC.

**Depends on:** KC-12 (silence clock), KC-36 (turn deadline and extensions), KC-45 (runner-level retry of a `session.error`).
**Also touches:** KC-61 (108). KC-61 ends an agent at once when **one** retry is scheduled far in the future (a daily quota). This ticket ends it when **many** short retries in a row never get an answer. Both read the same `session.status {type: retry}` event in `wait_idle`. **Do not run KC-61 and KC-64 as parallel rounds**: they edit the same lines. If KC-61 has landed first, put this check in its `session.status` branch, right after the quota check.

---

## How we met it

1. **+13.** Both sessions are created and prompted (`kilo-serve.log`
   08:30:36, `message=stream providerID=bynara modelID=nex-n2.5-pro`).
2. **+74.** The first retry, in both sessions:

       {"type": "session.status", "properties": {"sessionID": "ses_f28502306ffe…",
        "status": {"type": "retry", "attempt": 1,
         "message": "Upstream temporarily unavailable (was malformed body: upstream sent an error chunk inside a 200 stream: The model service is temporarily unavailable. Please try again.). Retry after 10s.",
         "next": 1790325106062}}}

   `next` is 10 s ahead, so this is a short retry, not a quota (KC-61 does
   not fire on it).
3. **+74 → +3593.** Kilo keeps retrying by itself. Each attempt is a
   `session.status busy`, then about 60 s later a `session.status retry`
   with `attempt` one higher. `laguna-s-2-1` reaches `attempt: 51`,
   `nex-n2-5-pro` reaches `attempt: 50`. One of nex's texts is
   "Upstream temporarily unavailable (was shared cooldown longer than this
   request's budget)". There is no `session.error` at any point, because Kilo
   never gives up on its own.
4. The only `message.part.updated` events in either stream are the 9 `text`
   parts of our own prompt. There is **no** assistant `step-start`,
   `reasoning`, `text` or `tool` part: 0 tokens out.
5. Every `busy` and every `retry` is an event of this session, so
   `wait_idle` resets `last_seen` on each one (≈ line 1212,
   `last_seen = time.monotonic()`). The gap is never more than ~76 s,
   far below `idle_event_timeout_sec = 900`.
6. **+3600.** `turn_timeout_sec = 3600` is reached. KC-36 asks the churn
   clock, which reports 0 files and 0 lines, so no extension is granted. The
   session is aborted, and both agents end `STALLED` with
   `no idle after 60m (0 files, 0 lines, unchanged for 60m)`.

Retries are normal while a provider is briefly overloaded: `mimo-v2-5` in
the same round had `attempt: 1` "free-model rate limit reached" about every
minute and got an answer each time (its `attempt` never went past 1,
because Kilo resets it after a success). What makes laguna and nex
different is that the `attempt` number kept growing with **no assistant
output in between**.

**It was most likely the bynara plan running out, not an outage.** The same
day `scripts/py_model_test.py` got the same "The model service is temporarily
unavailable. Please try again." from 10 of 12 bynara models, three runs in a
row. The operator then found the plan on the bynara site used up, and after the
renewal the same script got no "unavailable" at all (KC-61 (108), the bynara
row of its provider table). So the text cannot tell an exhausted plan from a
real transient 503. This ticket does not try to: it ends the agent the same
way for both, and the message says where to look (§4c).

## The rule

A turn ends with a new error, `ProviderUnavailable`, when Kilo reports
`session.status {type: retry}` with `attempt >= provider_retry_max_attempts`
and this session has produced **no assistant part since the retries
began**.

- Kilo's own `attempt` counter is used as is. It is 1 after every success
  (mimo), so a provider that answers now and then never reaches the limit.
- As a second guard, a local counter counts retries since the last
  assistant part. It covers a Kilo build that does not send `attempt`, and
  a build that does not reset it.
- `provider_retry_max_attempts = 10` by default. In round 106 one attempt
  took ~69 s (51 in 3519 s), so 10 attempts is ~12 min, instead of 60 min
  in round 106. `0` turns the rule off, which is today's behaviour.

## What must change — file by file

Line numbers are at `75432a2`. Find by the quoted anchor if they drifted.

### 1. `tools/contest/kilo_client.py` — `wait_idle` counts retries without output

**Where:** `def wait_idle(self, tap, session, timeout, *, …)` (≈ line 1086).
The event loop is at ≈ lines 1177–1255.

**Signature:** add one keyword-only parameter, last, off by default:

```python
                  max_retry_attempts: int | None = None) -> IdleResult:
```

If KC-63 has landed, it comes after `since`. The order does not matter,
since both are keyword-only.

**Filter:** `wanted()` (≈ line 1164) passes a `session.status` only while
the silence clock is on (`return silence is not None or etype in _SESSION_EVENTS`).
When `max_retry_attempts` is set, `session.status` and
`message.part.updated` must pass too. The first is counted, and the second
resets the count:

```python
            return (silence is not None or etype in _SESSION_EVENTS
                    or (bool(max_retry_attempts)
                        and etype in ("session.status", "message.part.updated")))
```

The runner always runs with the silence clock on, so this matters only for
a caller without one (tests, `variant.hello_probe`).

**State:** before the loop, next to `open_parts: dict = {}` (≈ line 1160):

```python
        # KC-64: retries Kilo reported since this session last produced output
        retries_without_output = 0
```

**Output resets the counter.** In the existing
`if etype == "message.part.updated":` block (≈ line 1216), after
`_track_open_part(...)`:

```python
                part = props.get("part") or {}
                # KC-64: any assistant-side part (a step, reasoning, text, a tool)
                # is the model answering; our own prompt's text parts carry the
                # user message's id, which Kilo never gives a step-start
                if part.get("type") in ("step-start", "reasoning", "tool", "step-finish"):
                    retries_without_output = 0
```

Do **not** reset the counter on `text` parts. The runner's own prompt comes
back as `text` parts (laguna: 9 of them, 0 tokens), and it would reset the
counter forever. A real answer always starts with a `step-start` part (see
mimo's +392.7 in round 106), so text alone is not needed.

**The check.** Insert right before `if etype == "session.error":`
(≈ line 1243):

```python
            if etype == "session.status" and max_retry_attempts:
                status = props.get("status")
                if isinstance(status, dict) and status.get("type") == "retry":
                    retries_without_output += 1
                    attempt = status.get("attempt")
                    count = attempt if isinstance(attempt, int) else retries_without_output
                    if min(count, retries_without_output) >= max_retry_attempts:
                        self._abort_quietly(session)   # stop Kilo's endless retry
                        return IdleResult(
                            status="error",
                            error={"name": "ProviderUnavailable",
                                   "data": {"message": str(status.get("message") or ""),
                                            "attempts": count}},
                            elapsed=time.monotonic() - started,
                            permissions=permissions, questions=questions)
                continue
```

- `min(count, retries_without_output)` means both counts must reach the
  limit. Kilo's `attempt` alone could come from before this wait (a retry
  that started in an earlier turn). The local count alone could miss the
  counter Kilo resets. Together they fire only on a provider that has
  failed N times in a row inside this wait.
- The trailing `continue` is right: a `session.status` never ends the wait
  in any other way. With the silence clock off it would otherwise fall
  through to `if etype != "session.idle": continue` anyway.
- If KC-61 has landed, its quota check is on the same event. Run it
  **first**: a daily quota must end at attempt 1, not at attempt 10.

### 2. `tools/contest/backend.py` — pass the keyword through

`KiloBackend.wait_idle` (≈ line 303): add `max_retry_attempts: int | None = None`,
and put it into `client_kwargs` only when truthy, the way `on_deadline` is
(≈ line 313). The `ContestBackend` protocol (≈ line 186) and
`OpenRouterBackend.wait_idle` (≈ line 496) take the same keyword.
OpenRouter ignores it, because it has no Kilo retry status.

### 3. `tools/contest/roster.py` + `contest.ini` — one key

- `CONTEST_KEYS` (≈ line 91, next to `"max_error_retries"`): add
  `"provider_retry_max_attempts"`.
- `ContestConfig` (≈ line 233, next to `max_error_retries: int = 2`):

  ```python
      #: KC-64: Kilo retries in a row with no model output before the agent
      #: ends ERROR provider_unavailable; 0 = off (wait for the turn deadline)
      provider_retry_max_attempts: int = 10
  ```

- The loader (≈ line 531, next to `max_error_retries=limit(...)`):
  `provider_retry_max_attempts=limit("provider_retry_max_attempts", 10),`.
- `contest.ini`, after `error_retry_backoff_sec` (line 75):

  ```ini
  # KC-64: a provider that fails this many retries in a row, with no model output
  # in between, ends the agent ERROR provider_unavailable (round 106: bynara
  # retried 51 times over 60 min). 0 = off.
  provider_retry_max_attempts   = 10
  ```

`limit()` (≈ line 452) is `safe_getint(...)` with a fallback, so `0` is
read as 0 (off). A negative value must be treated as 0 in `_wait_turn`
(§4a checks `> 0`).

### 4. `tools/contest/runner.py` — wire it in and name the end

**a. The call.** `_wait_turn` (≈ line 959), in `wait_kwargs` (≈ line 978):

```python
    attempts = int(getattr(config, "provider_retry_max_attempts", 0) or 0)
    if attempts > 0:
        wait_kwargs["max_retry_attempts"] = attempts
```

**b. Not retried by the runner.** In `run_agent`, in the
`elif idle.status == "error":` block (≈ line 1511), the KC-45 code decides
whether to send `RETRY_PROMPT`: `retryable = _retryable(idle.error)`
(≈ line 1547). `ProviderUnavailable` must **never** be retried. Kilo already
tried 10 times, and the runner's `max_error_retries = 2` with its backoff
would add two more full rounds of that. Add a helper next to
`_is_overflow` (≈ line 284):

```python
def _is_provider_unavailable(error) -> bool:
    """KC-64: wait_idle ended the turn after too many provider retries."""
    return isinstance(error, dict) and error.get("name") == "ProviderUnavailable"
```

and change the retry guard:

```python
                down = _is_provider_unavailable(idle.error)
                if not overflow and not down and retries_used < int(config.max_error_retries):
```

**c. The message.** In the fallback right below it (anchor
`# an overflow already set its own `error` and `state` above;`,
≈ line 1590):

```python
                if down:
                    data = (idle.error or {}).get("data") or {}
                    error = (f"provider_unavailable after {data.get('attempts')} retries: "
                             f"{_brief(data.get('message') or '')}"
                             f"{_PLAN_HINT}")
                    state = AgentState.ERROR
                elif not overflow:
                    ...  # today's fallback, unchanged
```

The KC-21 harvest after this block still runs. If the provider failed
**mid-turn** after the agent committed, the commit is scored. Uncommitted
work is KC-41's case (the deadline commit); do not handle it here.

`_PLAN_HINT` is a module constant next to the helper:

```python
#: KC-64: bynara answers an exhausted plan with the same "temporarily
#: unavailable" 503 as a real outage (KC-61, 2026-09-25), so the end says
#: where to look instead of guessing which one it was.
_PLAN_HINT = " (can be an exhausted plan — check the provider's site)"
```

Keep it in the message even when `_brief` cut the provider text: the hint
goes after the cut, never inside it.

**d. The summary.** `last_error` starting with `provider_unavailable`
groups these agents in the round table, the same way KC-61 groups
`provider_quota`. No renderer change is needed.

## `tests/_kilo_fake.py` — a turn that only retries

Add the scenario key `retries` (int, default 0) and `retry_message` (str).
In the turn's emit loop (the method that handles `names`, near
`pause_before_idle_sec` ≈ line 518), before the events:

```python
        # KC-64: the provider keeps failing — Kilo's own retry loop, as round
        # 106 showed it for bynara: busy, then retry with attempt n
        for n in range(1, int(turn.get("retries") or 0) + 1):
            self._emit({"type": "session.status",
                        "properties": {"sessionID": session.id,
                                       "status": {"type": "busy"}}})
            self._emit({"type": "session.status",
                        "properties": {"sessionID": session.id,
                                       "status": {"type": "retry", "attempt": n,
                                                  "message": turn.get("retry_message", "Upstream temporarily unavailable"),
                                                  "next": int((time.time() + 10) * 1000)}}})
```

Emit them with no delay, so tests stay fast. The rule counts events, not
time.

## Tests — where they go and what they check

New file `tests/test_contest_provider_unavailable.py`, docstring
`"""KC-64: a provider that keeps failing ends the agent after N retries."""`.
Use the `_probe` helper pattern from `tests/test_contest_kilo_client.py`
(≈ line 124), or import it if it is importable.

1. `test_ten_retries_without_output_end_the_wait`: a turn with
   `retries: 10` and no idle (`"idle": false`). `wait_idle(...,
   max_retry_attempts=10)` returns `status == "error"`,
   `error["name"] == "ProviderUnavailable"`, `error["data"]["attempts"] == 10`,
   and the fake recorded the abort (`h.fake.recorded_abort_for(h.session.id)`,
   `tests/_kilo_fake.py` ≈ line 342).
2. `test_nine_retries_do_not`: `retries: 9`, then `busy`, `idle`. The wait
   returns `idle`.
3. `test_output_between_retries_resets_the_count`: 6 retries, one assistant
   `step-start` part, then 6 more retries, then idle. Returns `idle`
   (6 + 6, never 10 in a row).
4. `test_off_by_default`: `retries: 12`, no `max_retry_attempts`. The wait
   does not end on them (with a short `timeout`, it returns `timeout`).
5. `test_the_runner_does_not_retry_provider_unavailable`: in
   `tests/test_contest_runner.py` style, a fake round where the agent's
   first turn is `retries: 10`, with `provider_retry_max_attempts = 10`.
   Expect `ERROR`, `last_error` starting `provider_unavailable after 10 retries:`
   and containing `temporarily unavailable`, ending with
   `(can be an exhausted plan — check the provider's site)`, exactly **one** turn in
   `turns.jsonl` (no `retry` turn), and no `RETRY_PROMPT` sent.
6. `test_roster_reads_provider_retry_max_attempts`: default 10, `0`
   accepted, and a value from a test's `contest.ini` read back.

Tier the new file with `python3 scripts/sync_test_tiers.py` and
`git add` the symlink (KC-60).

## Out of scope

- Switching the agent to another provider or model.
- Probing providers at intake. KC-61 §5 adds a hello probe for a named
  variant, and a provider that is down then gives a "no answer" note there.
- KC-61's quota (one retry far in the future).

## Acceptance

- [ ] Replaying laguna's round-106 stream (busy/retry pairs, `attempt` 1→51, no assistant part) ends the agent at attempt 10 with `ERROR provider_unavailable after 10 retries: … temporarily unavailable … (can be an exhausted plan — check the provider's site)`, and the session is aborted.
- [ ] mimo's pattern (a retry at `attempt: 1` about every minute, each followed by output) never ends the agent.
- [ ] `ProviderUnavailable` is never sent `RETRY_PROMPT`.
- [ ] `provider_retry_max_attempts = 0` gives today's behaviour, event for event.
- [ ] `tests` and `tests_bugfix` green, sequentially; `CollectBridge._shrink` byte-identical.
