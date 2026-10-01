# KC-71 — a subagent's permission is answered, and its work keeps the agent alive

**Status:** landed — by hand, reproduced live with mimo-v2-5 before and after the fix, 2026-09-27; found 2026-09-27 reading round 87's mimo-v2-5 STALLED
**Severity:** HIGH
**Round:** 117
**Size:** S
**File:** `tools/contest/kilo_client.py`
**Depends on:** KC-12 (landed: the silence clock), KC-47 (landed: an open `bash` widens it), KC-63 (landed: a wait reads only its own session).

Ground rules, as in every ticket: `CollectBridge._shrink` stays byte-identical, and nothing runs against a live provider config.

## Problem

Kilo's `task` tool runs a subagent in a session of its own —
`session.created` with `info.parentID` naming the agent's session. The
subagent's tools ask for permissions like the agent's do. `wait_idle` read
only events whose `sessionID` is the agent's own, so:

1. **A subagent's permission was never answered.** The subagent waits for the
   reply for ever; the agent waits for the subagent.
2. **The subagent's work was silence.** Its events did not reset KC-12's
   clock, so the agent looked idle for as long as the subagent ran.

Round 87, mimo-v2-5 (`contest-out/87/mimo-v2-5/events.jsonl`):

| Time | Event |
|---|---|
| 110 s | mimo calls `task` ("Explore FL-1 timing patterns"); the child session starts |
| 110 – 306 s | the child works: git, grep, reads of the tests |
| 301 s | the child asks `external_directory` for `~/.local/share/kilo/tool-output/*` — Kilo's own file holding a command's truncated output |
| 301 – 1010 s | no reply; the agent's own session sends nothing; only `server.heartbeat` |
| 1010 s | `no event for 900s` → **STALLED**; 0 files, 0 commits, `permissions.asked = 0` |

Every earlier round's events were scanned the same way (`contest-out/*/*/events.jsonl`):
this is the only child-session ask on the machine, so the bug was latent until a
model reached for `task`.

## Acceptance

- [x] `session.created` / `session.updated` with `info.parentID` equal to the
      agent's session — or to one of its subagents' — make that session the
      agent's for the rest of the wait (`_child_session`).
- [x] A subagent's `permission.asked` / `permission.v2.asked` goes through the
      same `on_permission` as the agent's own, and the reply is sent for the
      subagent's session (the legacy `/session/{id}/permissions/{pid}` route
      names the child); it is counted in the turn's `permissions`.
- [x] A subagent's `question.asked` is rejected like the agent's own.
- [x] With the silence clock on, every subagent event resets it, and a `bash`
      the subagent runs holds it open as KC-47 does for the agent's own.
- [x] The subagent's own `session.idle` / `session.error` never end the
      agent's turn; a session that is not the agent's child is still ignored.

## What landed

- `tools/contest/kilo_client.py` — `_CHILD_ASKS`, `_child_session`; in
  `wait_idle`, a `children` set filled from `session.created`/`updated`, the
  filter lets a child's asks through (and all its events with the clock on),
  the loop answers a child's ask on the child's `SessionRef` and treats its
  other events as liveness only.
- `tests/_kilo_fake.py` — a turn's `subagent`: a child session with
  `parentID`, its busy beats (`work_sec`), its permission and question (both
  block until answered), its own `session.idle` and `session.turn.close`.
- `tests/test_contest_subagent_asks.py` — the child's permission answered and
  the turn idle; the legacy reply route names the child; the child's idle does
  not end the turn; the child's question rejected; a 3 s working child under a
  1 s silence clock; a neighbour's ask still ignored; grandchildren; what is
  not a child. Against the pre-fix client, the four behaviour tests fail.

## Live reproduction, 2026-09-27

A throwaway clone of this branch at KC-70 (`53fd8f3`), one agent —
`kenary/mimo-v2-5:free@high` — on a scratch ticket that tells it to hand one
`read` of `~/.local/share/kilo/tool-output/<file>` to a `task` subagent, the
silence clock cut to 240 s. The same ticket, twice: once with the pre-KC-71
`kilo_client.py`, once with this one.

| | before (pre-KC-71 client) | after (this ticket) |
|---|---|---|
| `task` called | 53 s | 30 s |
| child session, `parentID` = the agent's | 54 s | 31 s |
| child asks `external_directory` `~/.local/share/kilo/tool-output/*` | 58 s | 52 s |
| the runner's reply | **none** | **same second**: `reject`, mechanical — `forbidden: … is at or under /home/renat/.local/share/kilo` |
| the subagent | waits | gets the refusal, ends at 58 s; its `session.idle` does not end the turn |
| the agent | **STALLED** `no event for 240s`, `permissions.asked = 0`, no commit — round 87's shape | **READY**, commit, `permissions.asked = 3`; the child's decision is in `decisions.jsonl` under the child's `sessionID` |

An earlier attempt on the same clone showed the same hang for 154 s (ask at
76 s, no reply) before it was stopped by hand; there mimo first read the file
itself — answered at once, as the agent's own ask always was — and only then
reached for `task`.

## Out of scope

- Whether the policy should let an agent read `~/.local/share/kilo/tool-output/*`
  (Kilo's own truncated output of that agent's command): today the mechanical
  layer refuses it as `forbidden` (it is under `~/.local/share/kilo`, the
  hard deny-list); the point here is that it is *answered*.
- The runner's `recent_tools` for the gate still reads the agent's session only.
