# KC-47 — a `bash` call that is still running is not silence: the stall clock waits out the call's own `timeout` instead of aborting the agent's test run at 300 s

**Status:** queued — found live 2026-09-23 in round 86: `sensenova-6-7-flash-lite-var1` was `STALLED — no event for 300s` in the middle of its own `pytest tests -n 4`, which the abort killed; twelve minutes later `mimo-v2-5` the same way.
**Severity:** HIGH (the one thing every ticket asks an agent to do — run the suite before committing — is what the silence window kills; five of the six silence stalls on this box are it)
**File:** `tools/contest/kilo_client.py`
**Symbol:** `KiloClient.wait_idle` (the KC-12 silence clock, `last_seen`)
**Round:** 91
**Size:** S
**Source:** round 86, `contest-out/86/sensenova-6-7-flash-lite-var1/events.jsonl`, the one `bash` part:

| t | `state.status` | |
|---|---|---|
| …750.753 | `running` | `python3 -m pytest tests -n 4 2>&1 \| tail -15`, `timeout: 2400000` (40 min) |
| …051.453 | `completed` | output: `User aborted the command` |

300.5 s apart. Between them the session sent nothing but `server.heartbeat`
(which is not this session's event and never reset the clock, by design —
KC-12, FL-1). `wait_idle` saw 300 s of silence, called `abort`, and the runner
wrote `STALLED`, `no event for 300s`. The agent had asked for 40 minutes;
`tests -n 4` takes 130–145 s on an idle box and more than 300 s on this one,
with eleven other agents and their suites on 8 cores.

Every silence stall in `contest-out/` (qwen25 and qwen26), with the last tool
part of the session:

| round | agent | last tool part | its output |
|---|---|---|---|
| 53 | hy3 | `bash` `python3 -m pytest tests -q --timeout=180 …`, `timeout: 600000` | `User aborted the command` |
| 59 | hy3 | `bash` `python3 -m pytest tests -q --timeout=180 -p no:cacheprovider …` | `User aborted the command` |
| 64 | mimo-v2-5 | `bash` `python3 -m pytest tests -n 4 -q --timeout=180 …`, `timeout: 300000` | `User aborted the command` |
| 64 | nex-n2-5-pro | `task` (a sub-agent), `status: error` | — |
| 86 | sensenova-6-7-flash-lite-var1 | `bash` `python3 -m pytest tests -n 4 …`, `timeout: 2400000` | `User aborted the command` |
| 86 | mimo-v2-5 | `bash` `python3 -m pytest tests -n 4 -q …`, `timeout: 300000` | `shell tool terminated command after exceeding timeout 300000 ms … retry with a larger timeout` |

Five of six are the runner killing the agent's test run. `mimo-v2-5` is the
sharp one: it asked for exactly 300 s, so Kilo's own kill and the silence
window fired in the same second (`completed` at …181.585, the runner's idle
at …184.809). With the bound in §2 the agent reads Kilo's "retry with a
larger timeout" and goes on; today the turn is over and four uncommitted
files sit in the worktree. KC-12 foresaw it
("a full `pytest` run … can legitimately produce no `/event` traffic for
minutes — that is not a stall") and answered with a tunable number. A number
cannot be right for both cases: 300 s is too short for a loaded suite and too
long for a dead stream. But the session *says* which case it is in — a `tool`
part in `running` is on the stream, with the timeout the agent asked for.

**Depends on:** KC-12 (the silence clock, landed `183de9b`), FL-1 (`e500d40`, the tap's silence accounting).
**Also touches:** `tools/contest/backend.py` (the three `wait_idle` adapters pass it through unchanged), `tests/test_contest_kilo_client.py`, `tests/_kilo_fake.py`

---

## What must change

### 1. `wait_idle` tracks this session's running `bash` parts

From `message.part.updated` events of this session whose `part.type ==
"tool"`: a part whose `state.status` is `running` (or `pending`) and whose
`part.tool == "bash"` is **open**; the same part id in any other status is
closed. The key is the part id.

### 2. While a `bash` part is open, the silence bound is the call's own

While at least one `bash` part is open, the silence clock's bound is not
`idle_event_timeout` but the largest of

- `idle_event_timeout`, and
- each open part's `state.input.timeout` (milliseconds → seconds) plus
  `idle_event_timeout` as grace — the tool's own kill comes first and its
  `completed` event resets the clock;

measured from that part's `running` event. A part without a numeric
`timeout` uses Kilo's `bash` default — read from the build, stated in a
module constant with the capture it came from — never "forever".

The overall `timeout` (`turn_timeout_sec`) still bounds everything,
unchanged: an agent that asks for a 40-minute `bash` on a 30-minute turn gets
the turn's end.

### 3. Nothing else is suspended

- A `task` part, an `edit`, a `read` — any non-`bash` tool — keeps KC-12's
  clock as today. (Round 64's `nex-n2-5-pro` sat in a failed `task`; that is
  KC-42's.)
- No open `bash` part → the loop is exactly KC-12's, event for event.
- Heartbeats still do not count as this session's events.

### 4. The stall says what it stopped

When the silence bound fires while a `bash` part is open, `IdleResult` carries
it (`open_tool = {"tool": "bash", "command": <first 120 chars>, "running_for": s}`)
and the runner's `last_error` reads
`no event for {n}s during bash: {command}` — so the next reader does not have
to dig in `events.jsonl` to learn the agent was running its tests.

## Out of scope

- Load on the box (`--max-parallel`, agents running stress suites) — operator's.
- Nudging a silent session with `continue` — KC-9.
- The 1800 s turn clock — KC-36.

## Acceptance

Through `_kilo_fake`, no real sleep longer than the fake's clock allows:

- [ ] A session emits a `bash` part `running` with `timeout: 600000`, then nothing for `idle_event_timeout + 1` s, then `completed` and `session.idle` → `status == "idle"`, `abort` never called.
- [ ] Same, but the silence runs past `600 + idle_event_timeout` s → `status == "timeout"`, `abort` called once, `open_tool.command` is the command.
- [ ] A `bash` part `running` with no `timeout` in its input uses the module default, not unbounded.
- [ ] A `task` part `running`, then silence past `idle_event_timeout` → `timeout` exactly as today.
- [ ] Two `bash` parts, one completed and one still running → the running one's bound applies; both completed → KC-12's bound.
- [ ] Another session's `bash` part in `running` does not change this session's clock.
- [ ] `turn_timeout_sec` shorter than the `bash` timeout → the wait ends at `turn_timeout_sec`.
- [ ] The runner's `last_error` for the round 86 shape is `no event for 300s during bash: python3 -m pytest tests -n 4 …`.
- [ ] Every existing KC-12 and FL-1 test passes untouched.
- [ ] `tests` and `tests_bugfix` green; `CollectBridge._shrink` byte-identical.
