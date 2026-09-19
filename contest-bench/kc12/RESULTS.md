# KC-12 round — `wait_idle(idle_event_timeout=)`, scored black-box

Ticket: `epic-tasks/51-kc12-stall-detection-wait-idle-aborts-when-no-event-arrives.md`
(round 51), base `ae124ea`. Eight files came in for six distinct entries:
the two Sonnet 5 files are the same diff, `kc12-Sensenova-6-7-var3.patch` is
a KC-16 submission (subject `KC16`, `cli.py`/`__main__.py`) filed in the
wrong folder — it goes to the KC-16 pile — and `kc5-deepseek-v1.patch` is the
KC-6 DeepSeek patch from round 45. Every entry applied cleanly with `git am`.

Method: one worktree per entry at the base with the patch applied
(`ingest_kc12.sh`), the mechanical checks of the ticket's self-check list,
then 19 scenarios (`scenarios_kc12.py`, `KC12_REPO=<worktree>`) driven
through the public client API against the *base* fake — the fake's own
emitter injects `file.edited` / `message.part.updated` / `server.heartbeat`
pulses and a neighbour session, so an entry's own additions to
`_kilo_fake.py` are not what is measured, except in s8 (the ticket's turn
shape). Then the entry's own `tests/test_contest_kilo_client.py` and the
unmodified `tests/test_contest_runner.py`, and finally the ideal's four
KC-12 tests (with the ideal's fake) on the entry's client.

The machine was shared with a live KC-16 hand round (three `kilo serve`
processes, load ≈ 3) during the run; three sub-second-margin scenarios
flaked once each (sonet5 s11, sn68 s7, sn67-var2 s3) and passed 4–6 of 4–6
reruns each — counted as passes.

## Scores

| entry | model | bench 19 | own tests | ideal's 4 | one commit | on-ticket files | runner tests unmodified | KC-1 tests unmodified | Py 3.10 |
|---|---|---:|---|---:|---|---|---|---|---|
| **sn68** | Sensenova 6-8 | **19** | 57 green | 4 | yes | yes | yes | yes | yes |
| sonet46-var2 | Claude Sonnet 4.6 var2 | 19 | 56 green | 4 | yes | yes | yes | yes | yes |
| sonet5 | Claude Sonnet 5 | 18 (s14) | 56 green | 4 | yes | yes | yes | yes | yes |
| sn67-var | Sensenova 6-7 var | 18 (s15) | 56 green | 4 | yes | yes | yes | yes¹ | yes |
| sn68-var2 | Sensenova 6-8 var2 | 18 (s8) | 57 green | 4 | yes | yes | yes | yes | yes |
| sn67-var2 | Sensenova 6-7 var2 | 18 (s16) | 58 green | **3** | yes | yes | yes | yes | yes |
| ideal | `kc12-ideal` | **19** | 58 green | 4 | yes | yes | yes | yes | yes |

¹ one removed line, in the module docstring's case list.

What the three losses are:

- **sn67-var2 — s16, a real bug.** `idle_deadline = float("inf")` until the
  first event of the session: a turn whose prompt is answered with nothing
  at all (no busy, no idle — the live shape of a provider that never
  responds) is never cut by the silence window and waits out the whole
  `timeout`. The runner's stall test passes by accident: `session.created`
  is on the tap ahead of the first turn. The ideal's fourth test is this
  case.
- **sn68-var2 — s8.** The fake's `pause_before_idle_sec` sleeps *first* and
  emits `session.status busy` *after* the pause — backwards to the ticket's
  sentence and to its own docstring; the entry's stall test still passes
  because the clock runs from the start of the wait.
- **sonet5 — s14, prose.** `_silence_watch` is deleted but still named in
  the runner's module docstring ("same guarantee `_silence_watch` used to
  provide"). **sn67-var — s15, prose.** "a session wedged 5 s into a 300 s
  wait" in the `wait_idle` docstring — the ticket's own
  `grep -n 'contest.ini\|300'` self-check finds it.

Everyone found the one non-obvious runner consequence: with `_silence_watch`
gone, the runner's unmodified stall tests (`idle_status == "stalled"`) need
`run_agent` to label an early `"timeout"` as `stalled` itself.

## Scenario matrix

```
scenario                                     sn67-var  sn67-var2  sn68  sn68-var2  sonet46-var2  sonet5  ideal
s0  signature verbatim                          ok        ok       ok      ok          ok          ok     ok
s1  stall cut at idle_event_timeout             ok        ok       ok      ok          ok          ok     ok
s2  omitted -> full timeout (today)             ok        ok       ok      ok          ok          ok     ok
s2b None == omitted                             ok        ok       ok      ok          ok          ok     ok
s3  any own event keeps it alive                ok        ok*      ok      ok*         ok          ok     ok
s3b unknown event type counts                   ok        ok       ok      ok          ok          ok     ok
s4  other session's events do not count         ok        ok       ok      ok          ok          ok     ok
s5  heartbeat without sessionID does not count  ok        ok       ok      ok          ok          ok     ok
s6  overall timeout still wins                  ok        ok       ok      ok          ok          ok     ok
s7  mid-flight silence is caught                ok        ok       ok*     ok          ok          ok     ok
s8  ticket's pause_before_idle_sec shape        ok        ok       ok      FAIL        ok          ok     ok
s9  permission still answered                   ok        ok       ok      ok          ok          ok     ok
s10 session.error still error                   ok        ok       ok      ok          ok          ok     ok
s11 two turns, clock per call                   ok        ok       ok      ok          ok          ok*    ok
s12 wall-clock jump ignored (monotonic)         ok        ok       ok      ok          ok          ok     ok
s13 no busy poll (< 0.8 s CPU for a 2 s wait)   ok        ok       ok      ok          ok          ok     ok
s14 runner: _silence_watch/inspect gone         ok        ok       ok      ok          ok          FAIL   ok
s15 no 300/contest.ini in the client            FAIL      ok       ok      ok          ok          ok     ok
s16 total silence from the start of the wait    ok        FAIL     ok      ok          ok          ok     ok
```
`*` = one load-induced flake, green on every rerun.

## Winner and ideal

Winner **Sensenova 6-8** (`kc12-Sensenova-6-8.patch`): 19/19, the most
complete tests (stall / eight windows of heartbeats / chatty neighbour), a
fake shape in the ticket's order. Sonnet 4.6 var2 ties on the bench with two
weaker tests (a 0.60 s window against a 0.65 s idle, no neighbour case).

The ideal commit is the winner reworked:

- `_SESSION_EVENTS` kept: the predicate only widens to "any event of this
  session" when the clock is on, so an omitted `idle_event_timeout` is
  KC-1's loop event for event, not just outcome for outcome;
- the fail-open `_seconds_or_none` helper dropped — `wait_idle` is a pure
  function of a number the caller resolved; `None` or a non-positive value
  is off, anything else is `float()`'s problem;
- the fake's shape reduced to the ticket's sentence (busy, sleep, return);
- a fourth test, from s16, so the sn67-var2 shape of the bug stays red.

Runner diff is the winner's as-is: `inspect`, `_WATCH_POLL`,
`_session_events`, `_silence_watch` and `_wait_turn(..., stall=)` gone,
`idle_status = "stalled"` on an early timeout.
