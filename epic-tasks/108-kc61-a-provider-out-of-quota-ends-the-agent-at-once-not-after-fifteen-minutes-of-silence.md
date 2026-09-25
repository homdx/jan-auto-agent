# KC-61 — a provider out of quota ends the agent at once, not after fifteen minutes of silence

**Status:** queued — found 2026-09-25 in round 107 (KC-60): all five `kenary` agents hit the provider's free daily limit on their first request and sat in `WAITING` for 15 min before the round called them `STALLED`.
**Severity:** LOW (the round loses no work: these agents never got to do any. The cost is that the operator's view is wrong for 15 minutes, and the right reason is missing: the report says "no event for 900s" instead of "out of quota until 00:00 UTC")
**File:** `tools/contest/kilo_client.py`, `tools/contest/backend.py`, `tools/contest/runner.py`, `tools/contest/roster.py`, `contest.ini`, `tools/contest/cli.py`, `tools/contest/variant.py`
**Symbol:** `KiloClient.wait_idle`, the runner's per-agent loop (the `session.error` / retry block), intake's `resolve_variants` / `hello_probe`
**Round:** 108
**Size:** M
**Source:** round 107, `contest-out/107/`:

`kilo-serve.log`, 09:22:59–09:23:00 UTC, once per agent (`hy3`, `agnes-2-0-flash`,
`agnes-2-5-flash`, `mimo-v2-5`, `step-3-7-flash`):

    message="stream error" providerID=kenary modelID=hy3:free
    error.error="AI_APICallError: free-model daily limit reached; the counter
    resets at 00:00 UTC. Top up from Rp1.000 at https://kenari.id/pay ..."

`hy3/events.jsonl`: Kilo does not raise a `session.error`. It turns the 429
into its own retry and tells the runner when it will try again:

    {"type": "session.status", "properties": {"status": {"type": "retry",
     "attempt": 1, "message": "free-model daily limit reached; ...",
     "next": 1790380800370}}}

`next` is 2026-09-26T00:00:00Z, which is 14 h 37 min after the event. Kilo
sends nothing more in that time. There are 0 `kenary` lines in the log
after 09:23, so the provider is not being hammered. The runner is the part
that is wrong:

- `wait_idle` counts the `session.status` as an event and then waits for the
  next one. `session.status` with `type: retry` has no branch of its own
  anywhere in `runner.py` or `kilo_client.py`.
- After `idle_event_timeout_sec` (900 in `contest.ini`) of silence the agent
  becomes `STALLED` with `last_error: no event for 900s`, and at 09:38 the
  session is cancelled. The quota message is in `events.jsonl` and in no
  field of `state.json`.
- Intake did not see it either. The round ran with `--variant high`, and the
  `'say: hello'` probe (KC-49) runs only for `variant = highest`. So an
  exhausted key reaches the round when the variant is named.

**Depends on:** KC-12 (stall detection), KC-19/KC-45 (retryable `session.error`s), KC-49 (intake probe).
**Also touches:** KC-55 (the gate already treats "`Retry-After` longer than `gate_retry_max_wait_sec`" as a quota reset, not a blip; this ticket is the same rule for the agents).

---

## How we met it (round 107, 2026-09-25, times UTC)

1. **Before the round.** The operator's model check (`scripts/py_model_test.py`)
   had sent code and tools prompts to the same five `kenary/*:free` models
   that morning, and round 103 had run on them the day before. kenari.id
   counts **one** daily free-model budget per key, shared by every `:free`
   model on it. (Its usage page later read: 3 753 requests, 364.3 M
   tokens, "Usage value Rp489 926", "Balance deduction Rp0".)
2. **09:22:47.** The round starts 10 agents with
   `--variant high --models …,agnes-2-5-flash:free,mimo-v2-5:free,step-3-7-flash:free,hy3:free,agnes-2-0-flash:free,…`.
   A named variant skips the KC-49 `'say: hello'` probe, so no request
   reaches kenary before the round.
3. **09:22:59–09:23:00.** Each kenary agent's first model call gets the 429.
   `kilo-serve.log` has one `stream error` per agent, and `events.jsonl` has
   one `session.status {type: retry, attempt: 1, next: 1790380800370}`
   (= 2026-09-26 00:00 UTC). After that, **nothing**: 0 kenary lines in the
   log after 09:23. Kilo is sleeping until midnight, not hammering the
   provider.
4. **09:23 → 09:38.** `state.json` shows all five `WAITING`. The heartbeat
   shows them as live agents with 0 files. The operator saw the quota text
   only by reading `kilo-serve.log` by hand.
5. **09:38.** `idle_event_timeout_sec = 900` runs out, and each is
   `STALLED: no event for 900s`, then `message=cancel`. The quota text is in
   no field of `state.json`, `turns.jsonl` or the summary.
6. **After.** The operator topped the key up. A `kenary/glm-4-7-flash:free`
   check then answered (code 15/15). `STALLED` is terminal, so neither the
   live round nor `--resume` can bring the five back. Only a second round
   (`--out contest-out/107-kenary`) can.

## Seen again in round 106 (same day, the local machine)

Round 106 (KC-59) ran on `kenary` too, with `--variant high`. Four agents
(`agnes-2-5-flash`, `step-3-7-flash`, `hy3`, `agnes-3-0-flash`) got the same
`session.status {type: retry, attempt: 1, message: "free-model daily limit reached; …", next: 1790380800…}`
between +1324 and +1444 s. That was **mid-work**, not on the first request.
Each sat 900 s and ended `STALLED: no event for 900s`. Two differences from
round 107 matter for this ticket:

- **Work was lost.** Here the key ran dry after the agents had worked for
  22–24 min. `../rounds/106-agnes-2-5-flash` and `../rounds/106-step-3-7-flash`
  each hold 5 uncommitted files, and `106-hy3` holds 2. `STALLED` exported
  none of them. So §3c's `ERROR provider_quota` must be followed by the same
  handling as any dirty end: the KC-21 harvest for a commit, and KC-41's
  deadline commit for uncommitted work once KC-41 lands. The Severity LOW
  above holds only for a key that is dry from the first request.
- **The gate ran on the same key.** The round's gate model was
  `hy3:free` on `kenary`, and it answered `gate unavailable: HTTP 429 — the
  reviewer is overloaded` (7 `gate_failed` over 4 agents in `state.json`).
  The gate's own quota is KC-55's scope, but intake (§5) should say when
  the gate model shares a provider key with agents in the round.

## What must change — file by file

Line numbers are at `e92f065`. Find by the quoted anchor if they drifted.

### 1. `tools/contest/kilo_client.py` — `KiloClient.wait_idle` sees the long retry

**Where:** `def wait_idle(self, tap, session, timeout, *, …)` (≈ line 1086).
The event loop is at ≈ lines 1177–1255. The anchor for the insertion is
`if etype == "session.error":` (≈ line 1243).

**Signature:** add two keyword-only parameters, both off by default, so every
existing caller (the runner, `variant.hello_probe`, tests) is byte for byte
today's:

```python
                  max_retry_wait: float | None = None,
                  quota_re: "re.Pattern | None" = None,
```

**Filter:** `wanted()` (≈ line 1164) lets `session.status` through only when
the silence clock is on (`silence is not None`). Add `"session.status"` to
the `etype in _SESSION_EVENTS` test *only when* `max_retry_wait` is set, so
the probe (no silence clock) also sees it.

**Insert** right before `if etype == "session.error":`:

```python
            if etype == "session.status" and max_retry_wait is not None:
                status = props.get("status") or {}
                if status.get("type") == "retry":
                    message = str(status.get("message") or "")
                    nxt = status.get("next")                      # ms since epoch
                    wait = (float(nxt) / 1000.0 - time.time()) if isinstance(nxt, (int, float)) else None
                    if (wait is not None and wait > max_retry_wait) or \
                            (wait is None and quota_re is not None and quota_re.search(message)):
                        self._abort_quietly(session)              # stop Kilo's own sleep-and-retry
                        return IdleResult(
                            status="error",
                            error={"name": "ProviderQuota",
                                   "data": {"message": message, "retryAt": nxt}},
                            elapsed=time.monotonic() - started,
                            permissions=permissions, questions=questions)
```

- Use `time.time()` (wall clock) for `next`, because Kilo sends epoch
  milliseconds. Keep `time.monotonic()` for everything else.
- The `ProviderQuota` name is the contract with §3. Nothing else in the
  codebase uses it today (`grep -rn ProviderQuota tools/` is empty).

### 2. `tools/contest/backend.py` — pass the two keywords through

**Where:** `KiloBackend.wait_idle` (≈ line 303). It builds `client_kwargs`
(≈ line 310) and calls `self._client.wait_idle(self._tap, session, timeout, **client_kwargs)`.
Add `max_retry_wait=None, quota_re=None` to its signature and put them into
`client_kwargs` only when not `None`, as `on_deadline` is (≈ line 313).
`OpenRouterBackend.wait_idle` (≈ line 496) and the `ContestBackend` Protocol
(≈ line 186) take the same two keywords. OpenRouter's subprocess backend
ignores them: it has no Kilo retry status.

### 3. `tools/contest/runner.py` — wire it in and name the state

**a. The call.** `_wait` (≈ line 960, anchor `"idle_event_timeout": silence or None,`)
adds to `wait_kwargs`:

```python
        "max_retry_wait": float(config.provider_retry_max_wait_sec) or None,
        "quota_re": _quota_re(config),
```

**b. The patterns.** Next to `_RETRYABLE_MSG_RE` (≈ line 188):

```python
def _quota_re(config) -> "re.Pattern | None":
    """KC-61: `[contest] quota_patterns`, `|`-separated, as one case-insensitive
    regex of literal phrases; None when the key is empty."""
    phrases = [p.strip() for p in str(config.quota_patterns or "").split("|") if p.strip()]
    return re.compile("|".join(map(re.escape, phrases)), re.IGNORECASE) if phrases else None


def _is_quota(error, quota_re) -> bool:
    """KC-61: a ProviderQuota from wait_idle, or any session.error whose text is a quota."""
    if isinstance(error, dict) and error.get("name") == "ProviderQuota":
        return True
    return bool(quota_re and quota_re.search(_error_message(error)))
```

**c. The decision.** In `run_agent`, `elif idle.status == "error":`
(≈ line 1511), **before** the KC-45 block (anchor
`# KC-45 §2a: KC-19's rules decide most`). It must come first, because
`_RETRYABLE_MSG_RE` matches `429` and would otherwise spend
`max_error_retries` against a key that is empty until midnight:

```python
                if not overflow and _is_quota(idle.error, _quota_re(config)):
                    data = (idle.error or {}).get("data") or {}
                    at = data.get("retryAt")
                    when = (time.strftime(" (retry at %Y-%m-%d %H:%M UTC)", time.gmtime(at / 1000))
                            if isinstance(at, (int, float)) else "")
                    error = f"provider_quota: {_brief(_error_message(idle.error))}{when}"
                    state = AgentState.ERROR
                else:
                    ...  # today's KC-45 retry block, unchanged, indented one level
```

The KC-21 harvest after it (anchor `# KC-21: a turn that died with a commit under it`)
still runs, so an agent that committed before its key ran dry is scored.

**d. The summary.** `RoundState.table_rows` (≈ line 638) already carries
`last_reason`. The `provider_quota:` prefix is enough for KC-7's
renderer to group them. Where the rows are printed (`cli.py` ≈ line 1408, `for row in state.table_rows():`),
add one line per provider:
`provider_quota: kenary × 5 — retry at 2026-09-26 00:00 UTC`.

### 4. `tools/contest/roster.py` + `contest.ini` — two keys

`CONTEST_KEYS` (≈ line 91), the dataclass (≈ line 233), the loader
(≈ line 531, next to `max_error_retries=limit(...)`), and `contest.ini`
after `error_retry_backoff_sec` (line 75):

```ini
# KC-61: a provider retry scheduled further out than this is a quota, not a blip:
# the agent ends ERROR provider_quota at once. 0 = off (wait for the silence clock).
provider_retry_max_wait_sec   = 300
# KC-61: quota phrases, |-separated, case-insensitive literals. Used when Kilo
# gives no retry time, and for a session.error. Transient texts must not be here.
quota_patterns                = daily limit | limit reached | quota | insufficient_quota | RESOURCE_EXHAUSTED | free-models-per-day | exceeded your current quota | insufficient credits | would exceed your available credits | top up | billing
```

`quota_patterns` is a string field. `limit()` is for numbers, so read it the
way the roster reads other strings (`grep -n "variant: str" tools/contest/roster.py`).

### 5. `tools/contest/cli.py` + `variant.py` — intake probes a named variant too

**Where:** `resolve_variants` (`cli.py` ≈ line 304). The named-variant
branch is at ≈ line 331 (anchor `if wanted != HIGHEST:`). Today it checks that
the variant is listed and appends the agent. It never calls `probe_for`.

**How:** after the `wanted not in listed` check, when `probe_for` is not
`None`, call `probe_for(agent)(wanted)` **once per distinct
`provider/model@variant`**, reusing the cache `highest` already keeps
(≈ line 315, "probed once however many agents ask for it"):

- The answer is `None` (hello came back): append the agent, as today.
- The answer is a quota (the text matches `_quota_re(config)`, or it is
  the `ProviderQuota` shape): append a failure
  `[<agent>] <model>: provider_quota — <text>`. The agent is not started,
  and the rest of the round runs.
- Any other answer: a note, not a failure. That is today's tolerance, since
  a named variant was never probed.

**`variant.hello_probe`** (≈ line 175): pass
`max_retry_wait=`, `quota_re=` into its `client.wait_idle(...)` (≈ line 205),
so a dry key answers in about a second instead of burning `HELLO_TIMEOUT_SEC = 60`
per model. `_error_text(idle.error)` then returns the quota message for the
check above.

One request per model is the whole cost: OpenRouter counts failed
requests against the free quota too.

## Tests — where they go

- `tests/test_contest_provider_quota.py` (new, docstring
  `"""KC-61: a provider out of quota ends the agent at once."""`).
- **wait_idle:** feed a fake `EventTap` the exact round-107 event
  (copy it from `contest-out/107/hy3/events.jsonl`, the `session.status`
  line, `next` rewritten to `now + 14 h`). Expect `status == "error"`,
  `error["name"] == "ProviderQuota"`, and one abort. With `next = now + 20 s`,
  the loop goes on. Find the existing fake tap with
  `grep -ln "class .*Tap" tests/`.
- **runner:** the fake backend returns that `IdleResult`. Expect `ERROR`,
  `last_error` starting with `provider_quota:` and containing
  `2026-09-26 00:00 UTC`, `retries_used == 0`, and no `RETRY_PROMPT` sent.
- **patterns:** each provider text in the table below goes through `_is_quota`.
  `temporarily unavailable`, `Server is busy` and
  `interrupted the response` must be False.
- **intake:** `resolve_variants` with `--variant high` and a `probe_for`
  that answers the kenary text, then `None`. The first gives a failure line,
  the second starts the agent.
- Tier with `python3 scripts/sync_test_tiers.py`, and **`git add` the
  symlink** (KC-60).

### Provider wording on record (for the default list and the tests)

| provider | status | text / code |
|---|---|---|
| kenari.id (round 107) | 429 via Kilo retry | `free-model daily limit reached; the counter resets at 00:00 UTC. Top up …` |
| OpenRouter | 429 | `Rate limit exceeded: free-models-per-day` (daily free cap, shared by every `:free` model) |
| OpenRouter | 402 | `This request would exceed your available credits …`, `error.metadata.limit_source` = `openrouter_credits` / `openrouter_key_limit` |
| Google Gemini | 429 | `RESOURCE_EXHAUSTED`, `Quota exceeded for metric: generativelanguage.googleapis.com/generate_content_free_tier_requests` |
| OpenAI-compatible | 429 | `error.code = insufficient_quota`, `You exceeded your current quota` (do not retry), versus `rate_limit_exceeded` (retry) |
| bynara.id (NaraRouter) | 429 | `type: rate_limited`. The daily token quota is per model tier, so one tier can be out while the others work |
| bynara.id | 503 | `The model service is temporarily unavailable. Please try again.`: **also what an exhausted plan looks like** (seen 2026-09-25, below) |

**bynara hides an exhausted plan behind "temporarily unavailable".** On 2026-09-25
`scripts/py_model_test.py` ran all 12 bynara models three times; 10 of them failed
every time with `The model service is temporarily unavailable. Please try again.`,
through both of the script's retries (15 s, then 30 s). The operator then found the
plan on the bynara site was used up. After the renewal the same 12 models, same
script, got no "unavailable" at all: 8 models 15/15 with tools ok, `laguna-s-2.1`
14/15, `nex-n2.5-pro` answered with broken code, `nemotron-3.5-lightning-free` hit
the 240 s timeout, and `jev` still rejects the request for its own reason. So this text is **not** a quota signal the runner can
trust — it is also a real transient 503 — and it must stay out of `quota_patterns`.
What the runner can do: after `max_error_retries` of it, write the error as
`provider_unavailable: … (can be an exhausted plan — check the provider's site)`
so the operator knows where to look, instead of a bare `session.error`.
That message is KC-64's (111) `provider_unavailable`: it ends an agent after
`provider_retry_max_attempts` such retries and carries this hint.

## Out of scope

- Moving the agent to another provider or key.
- Telling apart per-key and per-tier quotas (bynara): each agent is classified on its own event.
- The gate's own quota path (KC-55).

## Acceptance

- [ ] A fake event stream with `session.status {type: retry, next: now + 14 h, message: "free-model daily limit reached ..."}`: the agent is `ERROR` with `last_error` starting `provider_quota:` within seconds, not after `idle_event_timeout_sec`. The session is aborted.
- [ ] `session.status {type: retry, next: now + 20 s}`: nothing changes, and the agent keeps waiting.
- [ ] A `next` that is missing, with each provider text in the table above: the quota rows end `provider_quota`. `temporarily unavailable` and `Server is busy` stay on today's retry path.
- [ ] `quota_patterns` set in a test's `contest.ini` replaces the default list: a made-up phrase matches, and a default phrase no longer does.
- [ ] Intake with `--variant high` and a probe that answers a quota text: the agent is not started, and the plan names it. A probe that answers `hello` starts it, as today.
- [ ] The round summary shows `provider_quota` with the provider and the reset time.
- [ ] No provider name or host appears in the runner's code. Every provider text lives in config defaults and tests.
- [ ] `tests` and `tests_bugfix` green, sequentially; `CollectBridge._shrink` byte-identical.
