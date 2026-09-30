# KC-81 — A silent Kilo server is named in the heartbeat before every agent stalls

**Status:** queued
**Severity:** MEDIUM
**File:** `tools/contest/runner.py`
**Symbol:** `_Heartbeat`
**Round:** 128
**Size:** S
**Source:** round 127, first start 2026-09-30 10:37
**Depends on:** —
**Also touches:** `tools/contest/roster.py`, `contest.ini`, `tests/test_contest_runner.py`

---

## Why

Round 127's first start: all 11 agents were PROMPTED at 10:38:13 and after that
nothing came. The events.jsonl files stopped at 10:38:13, and so did
`contest-out/127/kilo-serve.log`. `kilo serve` was alive at 100 % CPU with no
connection to any provider. The heartbeat printed
`WAITING … 0% 0f` for all eleven, which is the same line a slow model prints.
The runner would have noticed only at `idle_event_timeout_sec` (900 s), and
then it would have aborted every agent one by one into the same hung server.
The operator found the hang by hand (log mtime, `top`, `ss`), killed the
server and restarted the round with `--fresh`. On the restart everything
worked.

A hang like this is a *server* fact, not an agent's. The runner can see it
earlier, and all at once: no agent has had an event, and the server's own log
has not grown.

## What to build

`_Heartbeat` (the once-a-minute line) gets one more check.

- **Silent server.** `kilo_silent_sec` (a `[contest]` key, default 600,
  `0` = off). The server counts as silent when both hold for that long:
  - no live agent has received an event;
  - `kilo-serve.log` (the `log_path` the round started the server with) has
    not grown in size.

  Either one moving means the server is fine. The log is checked **only**
  when the events have gone quiet; one quiet agent among busy ones is the
  stall logic's business, not this check's.
- **What it prints.** One `WARNING` line, once per silent spell. It prints
  again only after something moves and then goes silent again:

      kilo serve silent 10m: pid 1866424, cpu 100%, log idle since 10:38:13, 11 live agents without an event — the server looks hung; stop it (kill <pid>, kill -9 if it stays) and restart the round with --fresh

  CPU comes from `/proc/<pid>/stat`, sampled across one heartbeat interval.
  Where that is unreadable (not Linux, or no pid), it says `cpu ?`. The line
  goes to the round log and also into `state.json` as `server_silent` (the
  seconds and the pid), so `contest status` shows it.
- **It never kills.** No signal is sent and no agent is aborted: the
  decision is the operator's. The agents' own stall edge is unchanged.
- With no `log_path` (a server the round did not start), only the event side
  counts and the line says `log ?`.

## Acceptance

    python3 -m pytest tests/test_contest_runner.py -q -k kilo_silent

The tests use the round's existing fake Kilo (`tests/_kilo_fake.py`) and a
fake clock or small windows; no real `kilo`:

- sessions prompted, no event at all, log file not growing past
  `kilo_silent_sec` → exactly one `kilo serve silent` warning, naming the pid
  and the log's last-change time; `state.json` has `server_silent`;
- same, but the log keeps growing → no warning;
- same, but one agent keeps receiving events → no warning;
- a silent spell, then an event, then a second silent spell → two warnings;
- `kilo_silent_sec = 0` → never;
- in every case the fake server process is still alive, and no abort was sent
  to any session.

Existing tests stay green:

    python3 -m pytest tests -q
    python3 -m pytest tests_bugfix -q

## Out of scope

Restarting the server by itself, or resuming the round. That can be a follow-up
once the warning has proven itself in real rounds.
