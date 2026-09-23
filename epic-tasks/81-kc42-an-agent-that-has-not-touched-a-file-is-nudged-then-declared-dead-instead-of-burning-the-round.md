# KC-42 — An agent that has not touched a single file is nudged, then declared DEAD, instead of burning the whole round

**Status:** queued — after KC-9 and KC-39, whose edges it completes from the other side. Found live 2026-09-21 in round 64.
**Severity:** HIGH (three of eight slots in round 64 produced nothing and were only found out about after the round ended; a dead slot costs the same wall clock as a working one and silently shrinks the field the round is supposed to compare)
**File:** `tools/contest/runner.py` (`run_agent` — the `WAITING` loop), `contest.ini`
**Symbol:** `run_agent`, `_first_touch_deadline` (new), `_dirty_tree`, `AgentState.DEAD` (new), `ContestConfig.first_touch_sec`, `ContestConfig.first_touch_nudges` (new)
**Round:** 81
**Size:** M
**Source:** round 64 (`contest-out/64/`, base `9912b78`). Three of the eight agents finished the round with a worktree byte-identical to the base:

| agent | how it ended | events in its `events.jsonl` | what it did |
|---|---|---|---|
| `nex-n2-5-pro` | `stalled` after 1357 s | **54 695** | spent the turn inside one `task` subagent call that ended `status: error`; never opened a file |
| `laguna-s-2-1` | `error` after 759 s | 29 363 | streamed text, ended on a provider error; never opened a file |
| `agnes-3-0-flash` | `error` after 336 s | 411 | two `permission.asked` / `permission.replied` pairs, then the provider errored (this half is KC-35) |

`nex-n2-5-pro` is the shape that matters: it was never silent, so
`idle_event_timeout_sec = 300` never fired; it was never idle, so KC-22's
continue branch never ran; and its tree was never dirty, so KC-39's diff
signature has nothing to compare. It was *busy* and *useless* for 22
minutes and nothing in the runner is watching for that. KC-9 covers the
turn that goes quiet, KC-22/KC-39 cover the turn that works and repeats
itself — this ticket covers the turn that never starts.
**Depends on:** KC-9 (queued — `classify_idle`; this ticket's nudge reuses its `CONTINUE_PROMPT` shape but fires on a different signal), KC-39 (queued — `max_sessions_per_attempt` and the live session-reset path, reused for the nudge's escalation).
**Also touches:** `tests/test_contest_runner.py`, `tests/_kilo_fake.py`, `tools/contest/cli.py` (`SUMMARY.md` and the round table grow a `DEAD` row), `contest-bench/kc42/`


**Seen again, and the cause is always the same (2026-09-23, round 86 run 4):**
`nex-n2-5-pro`'s `task` sub-agent (`agent=explore mode=subagent`) was refused by
its provider: "the model's provider rejected the request. check the model id,
request fields, and context length". The same error appears once per round in
`kilo-serve.log` of rounds 64, 74 (qwen26), 85 and 86. This ticket's nudge and
`DEAD` answer the slot it wastes. Why the sub-agent's request is refused
(context length or a request field the provider rejects) is not established.
Worth checking before this lands: whether a contest session should be allowed
to spawn `task` sub-agents at all.

---

## What happens today

`run_agent`'s `WAITING` loop watches two clocks and one stream: silence
(`idle_event_timeout_sec`, → `STALLED`), the whole turn
(`turn_timeout_sec = 1800`, → `STALLED`), and the event stream for
`session.idle` / `session.error`. None of the three asks the one question
that separates a working agent from a dead one: **has the worktree
changed at all?**

So an agent that streams tokens forever without editing anything is
indistinguishable, to the runner, from an agent that is doing the ticket.
It holds its slot for the full `turn_timeout_sec`, it is counted in the
round's field, and it turns up as a plain `STALLED` at the end — the same
label as `mimo-v2-5`, which stalled holding 403 lines of passing code.

## What must change

1. **First-touch deadline.** A new clock inside the `WAITING` loop:
   `first_touch_sec` (default 420, a third of `turn_timeout_sec`) from the
   turn's `sent_at`. It is disarmed for good the first time `_dirty_tree(ws)`
   is non-empty or `_commits_above(ws) > 0` — once an agent has touched
   anything, this ticket is done with it and KC-22/KC-36/KC-39 own the rest
   of the turn.
2. **The nudge.** When the deadline passes with an untouched tree, send one
   prompt into the session naming the state plainly and without a critique:
   the files the ticket declares (`declared_files(ticket_path)`, already
   used by `harvest`), that nothing has been modified yet, and that the
   turn's clock is running. Re-arm the deadline. Repeat at most
   `first_touch_nudges` times (default 1).
3. **Escalation, borrowed rather than invented.** If the budget of nudges is
   spent and the tree is still untouched, do exactly what KC-39 does for a
   repeating diff — `client.abort(session)`, a fresh session with the same
   model and rules, `run.attempt` unchanged — subject to KC-39's own
   `max_sessions_per_attempt`. A model wedged in a broken tool loop
   (`nex-n2-5-pro`'s `task` call) cannot be talked out of it inside the
   session that is wedged.
4. **`AgentState.DEAD`.** When the escalation is unavailable or has already
   been used and the tree is *still* untouched, end the turn as `DEAD`: stop
   the clock, release the slot, abort the session, write the state. `DEAD`
   is terminal, is never harvested (there is nothing to harvest), and is
   reported separately from `STALLED` — the round's table must show "three
   agents never started" as its own number, not folded into the stalls.
5. **The round does not wait for it.** A `DEAD` agent stops contributing to
   the round's wall clock the moment it is declared.

## Acceptance

- [ ] `tests/test_contest_runner.py`: an agent that emits events continuously
      and never writes a file → one nudge at `first_touch_sec`, a session
      reset at the second deadline, `DEAD` at the third; `turns.jsonl`
      records all three.
- [ ] An agent that writes a file **before** the first deadline → no nudge
      ever, and the first-touch clock is not re-armed for the rest of the
      turn even if the tree later goes clean again (a `git checkout` by the
      model does not resurrect the deadline).
- [ ] An agent that writes a file **after** a nudge → no escalation, no
      `DEAD`; the turn proceeds through the normal edges.
- [ ] A `DEAD` agent is absent from the harvest entirely, appears in
      `SUMMARY.md` as `DEAD`, and does not hold the round open.
- [ ] `first_touch_sec = 0` → feature off, behaviour identical to the tree
      before this ticket (regression guard).
- [ ] **Replay against real data, required.** Round 64's
      `contest-out/64/*/events.jsonl` holds all three cases plus five
      counter-examples. Add `contest-bench/kc42/replay64.py`: feed each
      agent's recorded event stream and the matching worktree state through
      the new clock offline, and assert it fires for exactly
      `nex-n2-5-pro`, `laguna-s-2-1` and `agnes-3-0-flash` and for none of
      the other five — including `glm-4-7-flash`, whose tree was flat for
      long stretches behind a live heartbeat but which *had* already
      touched files (so the deadline must be disarmed, not merely
      not-yet-fired). Record it in `contest-bench/kc42/RESULTS.md`.
- [ ] **Live probe, required.** The replay proves the predicate; it cannot
      prove that a real wedged model reacts to a nudge. Build
      `contest-bench/kc42/live_probe.py` in `contest-bench/kc6/
      live_smoke.py`'s style: a ticket whose first instruction is a tool
      call that cannot succeed, against at least two roster models, with
      `first_touch_sec` cut to ~90 s. Confirm by hand that the nudge
      reaches the session, that the reset produces a second `session_id` in
      `state.json`, and that a `DEAD` agent really stops costing wall time.
- [ ] `python3 -m pytest tests -n 4 -q --timeout=180 && python3 -m pytest tests_bugfix -n 4 -q --timeout=180` green (sequentially).

## Out of scope

- Re-filling a reclaimed slot with another model mid-round. It makes a
  round's cost unpredictable and its timing unreproducible; the slot is
  closed, not replaced.
- Judging *quality* of early work. The predicate is "touched anything at
  all", deliberately the dumbest possible test — anything smarter would
  start rejecting exploratory work, which is the opposite of the goal.
- The provider-side errors behind `laguna-s-2-1` and `agnes-3-0-flash`
  (KC-35, KC-37). This ticket makes them cheap, it does not fix them.
- Counting tokens, tool calls or elapsed reasoning as progress signals —
  `nex-n2-5-pro` would have looked healthy on every one of them.

## Ground rules (same as every round)

- Do not touch `CollectBridge._shrink`.
- No test starts a real `kilo` or calls a live provider — `contest-bench/`
  is not `tests/` and is run by hand.
- Do not edit `epic-tasks/` (other than this round's own ticket file).
- One commit, no push; a test ships with the change and fails without it.
