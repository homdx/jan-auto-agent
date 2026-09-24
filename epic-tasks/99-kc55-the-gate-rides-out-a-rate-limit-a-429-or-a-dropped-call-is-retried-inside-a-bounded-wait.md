# KC-55 — the gate rides out a rate limit: a 429 or a dropped call is retried inside a bounded wait, and intake says when the gate shares an endpoint with the roster

**Status:** queued — found live 2026-09-24 in round 66 (base `01e7a40`, 12 agents, 8 of them on `kenary`); asked by the operator the same day. Takes over KC-37 §1 (see "KC-37" below).
**Severity:** HIGH (in nine recorded rounds the gate answered 4 times out of 52 calls: 48 `gate-failed` rejects, 4 `gate` verdicts; see the table)
**File:** `tools/contest/policy.py`, `tools/contest/roster.py`, `tools/contest/cli.py`, `contest.ini`
**Symbol:** `Policy._ask_gate`, `Policy._default_completion`, `Policy.__init__`, `Policy.record`, `Decision`, `ContestConfig` (four new `gate_*` fields), `gate_worst_case_sec` (new), `_build`, `intake`, `_check_offer`, `_print_plan`
**Round:** 99
**Size:** M
**Depends on:** KC-3 (the gate, `1499cd8`), KC-6 (policy layers, `e8c6ad3`), KC-12 (the silence clock in `wait_idle`), KC-25 (`GET /provider` at intake), RUN-10 / AUTO-RATE-1 (the retry machinery in `tools/llm_stream.request_completion`, already in the tree).
**Also touches:** `tests/test_contest_policy.py`, `tests/test_contest_cli.py`, `tests/test_contest_roster.py`

---

## Source — what happened, with the evidence

### Round 66 (2026-09-24, running while this was written)

`contest-out/66/glm-4-7-flash/decisions.jsonl`, the only gate call in the first 10 minutes:

```json
{"layer": "gate-failed", "reply": "reject",
 "reason": "gate unavailable: RuntimeError",
 "gate_elapsed": 0.78, "gate_model": "hy3:free",
 "command": "find /home/renat/Project/opensource/github/agent-offline -name \"runner.py\" -path \"*/contest/*\" 2>/dev/null | head -5"}
```

A reply after 0.78 s is no verdict. It is an HTTP error the gate turned into a
`reject` on its first attempt. The gate is `[contest_gate_llm]` in
`contest.local.ini`:
`base_url = https://kenari.id/v1`, `model = hy3:free`. The same endpoint and key
serve 8 of the round's 12 agents (`--models agnes-2-5-flash:free,mimo-v2-5:free,
step-3-7-flash:free,hy3:free,laguna-s-2-1:free,agnes-2-0-flash:free,glm-4-7-flash:free`
all resolve to Kilo provider `kenary`, whose `options.baseURL` in `GET /provider`
is `https://kenari.id/v1`).

Meanwhile every `kenary` agent's `events.jsonl` shows Kilo's own retries on
that endpoint. From `session.status` events, the first 10 minutes:

```
29 × {"type": "retry", "attempt": 1, "message": "free-model rate limit reached; slow down and retry"}
     {"type": "retry", "attempt": 2..7, "message": "upstream unavailable for model laguna-s-2-1:free"}
```

Kilo retries the agents' calls and the agents keep working: tool calls keep
rising after every retry. The gate retries nothing.

The four `sensenova/*` agents and `kilo/nex-agi/nex-n2.5-pro:free` sit on
other endpoints. They show **0** retries in the same window.

### Every recorded round (`contest-out/*/decisions.jsonl`)

| round | `mechanical` | `gate` (a verdict) | `gate-failed` | reasons |
|---|---|---|---|---|
| 53 | 0 | 0 | 1 | `RuntimeError` (gate still `some/model`) |
| 61 | 49 | 0 | 2 | `empty reply` × 2 |
| 63 | 45 | 0 | 4 | `empty reply` × 4 |
| 64 | 57 | 0 | 1 | `empty reply` |
| 66 | 9 | 0 | 1 | `RuntimeError` (a 429, see below) |
| 72 | 63 | 0 | 2 | `empty reply` × 2 |
| 84 | 18 | 0 | 6 | `empty reply` × 6 |
| 86 run 3 (KC-37) | — | 1 | 12 | `empty reply` × 11, `RuntimeError` × 1 |
| 91 (KC-47) | 155 | 3 | 19 | `empty reply` × 12, `RuntimeError` × 7 |

The gate model was `hy3:free` on `kenari.id` every time.

### Reproduce by hand (the operator's box; never in a test)

```bash
K=$(grep -m1 '^api_key' contest.local.ini | sed 's/.*= *//')
for i in 1 2 3; do
  curl -s -m 30 -o /dev/null -w "%{http_code} %{time_total}s\n" \
    https://kenari.id/v1/chat/completions \
    -H "Authorization: Bearer $K" -H "Content-Type: application/json" \
    -d '{"model":"hy3:free","messages":[{"role":"user","content":"say ok"}],"max_tokens":5}'
done
```

The result while round 66 ran: `429 0.46s`, `429 0.40s`, `429 0.42s`. One
minute later the same call came back `200` with:

```
x-ratelimit-limit: 15
x-ratelimit-remaining: 8
x-ratelimit-reset: 1790217987
```

One key allows 15 requests per window. Eight agents plus the gate spend them.
The 429 is transient: a few seconds later the same call succeeds.
`tools/llm_stream.request_completion` raises it as
`RuntimeError("HTTP 429 from https://kenari.id/v1/chat/completions: …")`, and
`_ask_gate` keeps only the class name (`gate unavailable: RuntimeError`). So
neither the agent nor the operator can see it was a rate limit.

---

## What happens today (the code)

`tools/contest/policy.py`:

```python
GATE_TIMEOUT = 60.0                                   # line ~79

def _default_completion(self, url, headers, payload, timeout, *,
                        stream=False, api_format="openai",
                        ssl_context=None, error_retries=0):
    """The default ``completion_fn``: ``request_completion``, fail fast."""
    return request_completion(url, headers, payload, timeout,
                              stream=stream, api_format=api_format,
                              ssl_context=ssl_context, error_retries=error_retries)

def _ask_gate(self, props, ctx):                      # line ~656
    ...
    try:
        reply = self._completion_fn(url, headers, payload, GATE_TIMEOUT,
                                    stream=False, api_format=api_format,
                                    ssl_context=..., error_retries=0)
    except Exception as exc:
        ...
        return Decision("reject", "gate-failed",
                        f"gate unavailable: {type(exc).__name__}", elapsed, None)
    ...
    verdict, reason = _extract_verdict(reply)
    if verdict == "allow":  return Decision("once", "gate", ...)
    if verdict == "reject": return Decision("reject", "gate", ...)
    return Decision("reject", "gate-failed",
                    f"gate unavailable: {text.strip()[:_GATE_FAIL_QUOTE] or 'empty reply'}", ...)
```

The retry machinery already exists and is not used here.
`tools/llm_stream.request_completion(..., error_retries, error_retry_wait_sec,
max_retry_after_sec, on_retry, _sleep_fn)` (AUTO-RATE-1, RUN-10) does the
following:
- retries 429, 402, 5xx and network errors (`TimeoutError`, `URLError`, …);
- waits the server's `Retry-After` (header or a "try again in Ns" body hint)
  on a 429, else `error_retry_wait_sec`;
- raises at once when the server asks for more than `max_retry_after_sec`
  (a quota reset, not a blip);
- raises at once on a non-retryable status (401, 403, 404, 400);
- takes `_sleep_fn`, so tests never sleep.

Every auto-mode caller uses it with `[loop] error_retries = 60,
error_retry_wait_sec = 10, max_retry_after_sec = 180` (`agents_128k.ini`
lines 149–164). Only the contest gate passes `error_retries=0`.

**Why the retry budget must be bounded:** the gate call runs synchronously
inside `KiloClient.wait_idle`'s loop (`kilo_client.py` ~line 990:
`reply, message = on_permission(event)`). `last_seen` is set when the
`permission.asked` event arrives, before the gate runs. While the gate waits,
the session produces no events, since it is blocked on our reply. So **every
second the gate spends counts against the silence clock**
(`idle_event_timeout_sec`, default 300 in `roster.py`, 900 in `contest.ini`),
and against `turn_timeout_sec`. A gate that waits longer than the silence clock
makes the runner declare its own agent `STALLED`. Each agent runs in its own
`ThreadPoolExecutor` worker (`runner.py` ~line 924), so one agent's gate wait
does **not** block the other agents.

---

## What must change

### 1. The gate's transport retries: four new `[contest]` keys

`contest.ini`, next to `gate_max_calls_per_session`:

```ini
# KC-55: a gate call that hits a rate limit or a dropped connection is retried
# (429, 402, 5xx, timeouts, connection errors — never 400/401/403/404).
# The wait is the server's Retry-After on a 429, else gate_retry_wait_sec.
# A Retry-After longer than gate_retry_max_wait_sec is a quota reset: no wait,
# the call fails at once. 0 retries = today's fail-fast gate.
# gate_deadline_sec caps one whole gate decision: every retry and re-ask.
gate_retries                 = 3
gate_retry_wait_sec          = 10
gate_retry_max_wait_sec      = 60
gate_deadline_sec            = 600
```

- `roster.py`: `ContestConfig` gets `gate_retries: int = 3`,
  `gate_retry_wait_sec: float = 10.0`, `gate_retry_max_wait_sec: float = 60.0`,
  `gate_deadline_sec: float = 600.0`.
  `_build` reads `gate_retries` with the existing `limit(...)` helper and the
  three times as floats (`float(scalar(key, default))`, a `ValueError` becomes
  a `RosterError` naming the key), and
  they join the known `[contest]` keys (an unknown key is still an error).
  A negative value is a `RosterError` naming the key.
- `Policy.__init__` gains `sleep: Callable | None = None` (default
  `time.sleep`), the same injectable pattern as `clock`.
- `_default_completion` loses its `error_retries=0` default. `_ask_gate`
  passes, from the config:
  `error_retries=config.gate_retries`,
  `error_retry_wait_sec=config.gate_retry_wait_sec`,
  `max_retry_after_sec=config.gate_retry_max_wait_sec`,
  `_sleep_fn=<the deadline-checked sleep of §3>`,
  `on_retry=<a closure that keeps the last message>`.
  Count attempts as **the number of `_sleep_fn` calls + 1**, not the number
  of `on_retry` calls. `request_completion` also calls `on_retry` on the
  quota-reset path, where it does not wait and does not call again. The count
  is a lower bound: the one-shot 400 strips in `_open` (`reasoning_effort`,
  `think` and the like) resend without a sleep. That is fine for a reason line.
  `_default_completion` forwards all five to `request_completion`. Do **not**
  write a second retry loop for transport errors: the one in `llm_stream` is
  the project's, and it already reads `Retry-After`.
- An injected `completion_fn` (every test's fake) receives the same keyword
  arguments, so a test can assert what was passed. The existing test
  `test_default_completion_builds_the_call_from_gate_settings` asserts
  `seen["error_retries"] == 0`. It is the one existing assertion this ticket
  changes: it becomes `== 3` (the config default), and the test also asserts
  the other three keywords. Change nothing else in that test.

### 2. An empty or unparsable reply is re-asked once (KC-37 §1, folded in)

`GATE_RETRIES = 1`, a module constant next to `GATE_TIMEOUT`. When
`_extract_verdict` yields neither `allow` nor `reject` (the empty body
included) and a re-ask is left, `_ask_gate` sends the same request again
after `GATE_RETRY_WAIT = 2.0` seconds, via `self._sleep`. That call has its
own transport retries from §1. The last attempt's verdict stands.

- A clean `reject` is a verdict and is **never** re-asked.
- A parsable reply on the first call means exactly one call.

### 3. The whole wait is bounded by a deadline, and intake checks it

A formula over the retry counts does not bound the wait. In
`request_completion(stream=False)` the body-read loop (`_read_attempt`, up to
`error_retries`) calls `_open()` again on every pass, and `_open()` has its own
`attempt` counter. So one call can make up to `(gate_retries + 1)²` requests:
16 with the defaults, not 4. The bound is a wall-clock deadline instead:

- `_ask_gate` takes `start = self._clock()` once, before the first call. That
  one start covers the re-ask of §2 too.
- The `_sleep_fn` it passes checks the deadline before each wait:
  `if self._clock() - start + wait > config.gate_deadline_sec: raise _GateDeadline(...)`,
  else `self._sleep(wait)`. The §2 re-ask wait (`GATE_RETRY_WAIT`) goes through
  the same check.
- `_GateDeadline` subclasses `Exception` **directly**, not `RuntimeError`,
  `OSError` or `ValueError`. That way neither `except` clause in
  `request_completion` swallows it: `_open` catches `HTTPError` and
  the network errors, and the read loop catches those plus `ValueError`.
  `_ask_gate` catches it and returns
  `reject` / `gate-failed`.
- Every wait is checked, so a decision stops no later than the deadline plus
  one request in flight. The worst case is:

```
gate_worst_case_sec(config) = gate_deadline_sec + GATE_TIMEOUT
```

With the defaults that is `600 + 60 = 660 s`, under `contest.ini`'s
`idle_event_timeout_sec = 900`. In `intake`, when the gate is on and
`idle_event_timeout_sec > 0`, a worst case of `idle_event_timeout_sec` or more
is an intake failure:

```
intake: gate retries can outlast the silence clock: worst case 660 s ≥ idle_event_timeout_sec 300 —
  lower gate_deadline_sec in contest.ini, or raise idle_event_timeout_sec
```

`policy.gate_worst_case_sec(config)` is the one place the number comes from,
so intake and the tests read the same value. `GATE_TIMEOUT` itself does not
change.

### 4. The reason says what went wrong, and the record says how hard it tried

- A transport failure's reason names the status when there is one, and the
  attempts:
  `gate unavailable: HTTP 429 (4 attempts, 31.2 s)`. The status is parsed
  from the `RuntimeError` text `HTTP <code> from <url>: …` with
  `r"\bHTTP (\d{3})\b"`. Without a status, the reason names the transport
  error: `gate unavailable: TimeoutError (4 attempts, 240.0 s)`. Note that
  `request_completion` wraps a network error in
  `RuntimeError("TimeoutError calling <url>: …")` (and `"… reading response
  body from …"`). So `type(exc).__name__` is `RuntimeError` here, and the name
  comes from the text instead, with `r"^(\w+Error) (?:calling|reading)\b"`.
  Only when neither pattern matches is the exception class used, as today.
- The deadline of §3: `gate unavailable: HTTP 429 (5 attempts, 598.0 s, deadline 600 s)`.
  The last status or error seen goes first, then the deadline.
  **Never the URL, never the body, never a key.** The reason is shown to the
  agent.
- With one attempt, the reason stays as today:
  `gate unavailable: RuntimeError` becomes `gate unavailable: HTTP 429`
  (the status only).
- An unparsable reply after the re-ask:
  `gate unavailable: empty reply (2 attempts)`.
- When the gate failed on a 429 or 5xx after all retries, the agent-facing
  reason ends with one sentence it can act on:
  ` — the reviewer is overloaded; a command inside your worktree needs no reviewer`.
- `Decision` gains `gate_attempts: int = 1`. `Policy.record` writes
  `"gate_attempts"` into `decisions.jsonl` only when it is `> 1`, so today's
  records stay byte-identical. `gate_elapsed` is the **total** wall time
  across all attempts and waits, measured with `self._clock`.
- The budget (`gate_max_calls_per_session`) counts **decisions, not
  attempts**. It is still checked once, up front. `runner.py`'s
  `on_permission` counting (`gated` / `gate_failed`) is unchanged: one
  decision is one unit.
- `logger.warning` in `_ask_gate` keeps its line and adds the attempt count.

### 5. Intake says when the gate shares an endpoint with the roster, and asks it once

This is the round 66 root cause: the gate is a different *model* from some
agents, but it sits on the same *key and endpoint* as eight of them. KC-37 §2
compares `model_id` and would miss a gate on `kenary/mistral-medium-3-5:free`
next to eight other `kenary` agents.

- In `_check_offer` (`cli.py`), the offer already comes from
  `GET /provider`. Each provider there has `options.baseURL`, e.g.
  `{"id": "kenary", "options": {"baseURL": "https://kenari.id/v1"}}`. When
  the gate's `base_url` host (`urllib.parse.urlsplit(...).hostname`) equals
  the `baseURL` host of the provider of **N ≥ 1** agents, print one
  `intake:` line on **stdout** as a warning, **not a failure**. The
  operator may have a paid key:

  ```
  gate: shares kenari.id with 8 agents (agnes-2-5-flash, mimo-v2-5, …) — a rate limit there hits the gate too;
    point [contest_gate_llm] in contest.local.ini at another endpoint
  ```

  Name at most 4 agents, then `…`. A provider with no `options.baseURL` does
  not match. Never print the key.
- Right after that check, ask the gate once, with the same request shape as
  `_ask_gate` (a fixed probe `user_msg`, e.g.
  `permission: probe\ncommand: true`). Use **one attempt, no retries**,
  timeout `GATE_TIMEOUT`, through `Policy`'s `completion_fn`. Two outcomes
  refuse the round:
  - **HTTP 401 / 403**:
    `intake: gate [contest_gate_llm] refused its key (HTTP 401) — set api_key in contest.local.ini`;
  - **HTTP 404**:
    `intake: gate model '<model>' not found at <host> (HTTP 404) — set model in [contest_gate_llm]`.

  Anything else only prints a `gate:` warning line and the round starts:
  a 429 or 5xx (transient, and exactly what §1 retries), a timeout, an
  unparsable reply. This runs only when `--no-gate` is off and the gate is
  not the placeholder `some/model`. The probe does not spend any agent's
  budget.

### 6. The plan line names the gate model (KC-37 §3, folded in)

`_print_plan`'s `("gate", "on"|"off")` becomes `("gate", "<model> @ <host>")`
/ `("gate", "off")`, e.g. `gate  hy3:free @ kenari.id`. Model id and host
only: no key, no path.

---

## KC-37

KC-37 (76) is `open` and not yet run. This ticket takes over its §1 (the
re-ask of an empty reply, here §2) and its §3 (the plan line, here §6), and
widens both:
- 429s and timeouts get retried, not only empty replies;
- the plan line shows the host as well as the model.

KC-37 §2 (intake refuses a gate model that the roster also runs, by
`model_id`) stays KC-37's and is **not** in this ticket. If KC-37 lands
first, fold its `GATE_RETRIES` loop into §1–§2 here and keep its tests green.
If this lands first, KC-37 shrinks to its §2. Its Status line must then say so.
That is a doc edit for the operator, not for this round.

## Acceptance

All tests are offline: an injected `completion_fn`, `clock` and `sleep`, and
a canned `GET /provider` dict. **No test opens a socket or sleeps real wall
time.** Where a test needs `request_completion`'s real retry loop, it
monkeypatches `tools.llm_stream.urllib.request.urlopen` (the one call site,
`llm_stream.py` ~line 1419) to raise
`urllib.error.HTTPError(url, 429, "Too Many Requests", {"Retry-After": "7"}, None)`
and passes `_sleep_fn` through `Policy(sleep=...)`.

- [ ] `tests/test_contest_policy.py`:
  - **429 then allow:** the real `request_completion`, the opener raising
    one 429 with `Retry-After: 7`, then returning an `allow` body → `once` /
    `gate`, `gate_attempts == 2`, the fake sleep called once with `7.0`,
    `gate_elapsed` includes the 7 s on the fake clock;
  - **429 every time** (4 calls, `gate_retries = 3`) → `reject` /
    `gate-failed`, the reason starts with `gate unavailable: HTTP 429 (4 attempts`,
    ends with the overload sentence, and contains no `http://`, `https://`
    or `Bearer`;
  - **Retry-After: 3600** → one call, no sleep, `reject` / `gate-failed`
    with `HTTP 429`: the quota-reset path;
  - **401** → one call, no sleep, reason `gate unavailable: HTTP 401`, no
    overload sentence;
  - **`gate_retries = 0`** → one call, reason `gate unavailable: HTTP 429`:
    today's behaviour, named;
  - **empty reply then allow** (fake `completion_fn`) → `once` / `gate`,
    `gate_attempts == 2`, the fake sleep called with `GATE_RETRY_WAIT`;
  - **empty reply twice** → reason `gate unavailable: empty reply (2 attempts)`;
  - **a clean reject first** → one call, `reject` / `gate`, never re-asked;
  - **budget:** `gate_budget_left == 1` with a 429-then-allow → the
    decision is the gate's (`once`), not `budget`;
  - **`decisions.jsonl`:** a one-attempt decision's record has no
    `gate_attempts` key (the record is byte-identical to today's); a
    two-attempt one has `"gate_attempts": 2`;
  - **the kwargs:** `test_default_completion_builds_the_call_from_gate_settings`
    asserts `error_retries == 3`, `error_retry_wait_sec == 10.0`,
    `max_retry_after_sec == 60.0` and that `_sleep_fn` is callable and calls
    the policy's injected sleep.
    This is the only edit to an existing test;
  - **`gate_worst_case_sec`:** the defaults give `660.0`;
  - **the deadline:** `gate_deadline_sec = 30`, a 429 every time with
    `Retry-After: 10` on a fake clock that the fake sleep advances → the
    call stops before a wait that would pass 30 s, `reject` / `gate-failed`,
    reason contains `deadline 30 s`, total fake sleep ≤ 30;
  - **a timeout:** the opener raises `TimeoutError` every time → the reason
    starts with `gate unavailable: TimeoutError (`, not `RuntimeError`;
  - **read-loop nesting:** the opener returns a body that is not JSON
    (`ValueError` in the read loop) every time, with `gate_deadline_sec`
    small → the decision ends at the deadline, not after
    `(gate_retries + 1)²` calls.
- [ ] Roster tests: the four keys load from `contest.ini` with the defaults
      above; a negative value → `RosterError` naming the key; an absent key
      → the default.
- [ ] `tests/test_contest_cli.py` (a canned offer and a fake gate `completion_fn`):
  - a gate `base_url` on the same host as 2 agents' provider `baseURL` →
    one `gate: shares <host> with 2 agents (…)` line on stdout, and the
    round runs;
  - a gate on a host no agent uses → no `shares` line;
  - the intake probe answering 401 → an `intake:` failure naming
    `contest.local.ini`, a non-zero exit, no session opened;
  - the probe answering 429 → a `gate:` warning line, and the round runs;
  - `--no-gate` → neither the probe nor the `shares` line;
  - `idle_event_timeout_sec = 300` with the default retries → an `intake:`
    failure naming `660 s` and `300`;
  - the plan line reads `gate  hy3:free @ kenari.id`.
- [ ] Every other existing test unmodified and green.
- [ ] `python3 -m pytest tests -n 4 -q --timeout=180`, then
      `python3 -m pytest tests_bugfix -n 4 -q --timeout=180`, run one after
      the other; both green.

## Out of scope

- `tools/llm_stream.py`: its retry loop is reused as is. Reading
  `x-ratelimit-reset` (sent by `kenari.id`, see above) as a wait hint would
  be a change there, in its own ticket.
- `GATE_TIMEOUT`, `GATE_SYSTEM_PROMPT`, and what the gate decides when it
  answers.
- KC-37 §2: refusing a gate model the roster runs, by `model_id`.
- `runner.py` and `kilo_client.py`: the silence clock stays as it is. §3
  keeps the gate inside it, rather than pausing the clock.
- Choosing a real gate model: `contest.ini` keeps `some/model`, and
  `contest.local.ini` is the operator's. No key is read, logged or printed.
- KC-28, KC-51, KC-52 and KC-53 (commands that should never reach the gate).
  They reduce how often the gate is asked. This ticket is about what happens
  when it is.

## Self-check before `append_task.py`

- [ ] `python3 --version` on the judge is **3.10.12**.
- [ ] Exactly **one** commit. Only the files in `**File:**` and the test files are touched.
- [ ] `git diff --stat <base>..HEAD` names only `tools/contest/policy.py`,
      `tools/contest/roster.py`, `tools/contest/cli.py`, `contest.ini`,
      `tests/test_contest_policy.py`, `tests/test_contest_cli.py`,
      `tests/test_contest_roster.py`, plus
      `.smoke_tests/` links. Never `epic-tasks/`, `contest.local.ini` or
      `tools/llm_stream.py`.
- [ ] `grep -n "error_retries=0" tools/contest/policy.py` is empty.
- [ ] No test sleeps real wall time or opens a socket. Every new test
      passes with the network down (`unshare -rn` with `lo` up).
- [ ] The new tests are red without the change.
- [ ] `python3 scripts/sync_test_tiers.py --check` clean.
- [ ] `CollectBridge._shrink` byte-identical.
