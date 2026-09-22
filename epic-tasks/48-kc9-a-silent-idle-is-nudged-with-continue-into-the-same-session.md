# KC-9 — A session that stopped without finishing — idle-but-cut or silent past `idle_event_timeout_sec` — is nudged with `continue`, same session, bounded

**Status:** queued — after KC-6 (round 45); lands before KC-7 is run live. Written against `docs/kilo-contest/PROBE.md`. Note added 2026-09-22: KC-22 (round 61) landed the base continue-on-idle-with-a-dirty-tree mechanism ahead of this ticket, including `max_continues_per_attempt` — so "no край gives the cut turn another `continue`" below is no longer accurate on its own; this ticket's remaining scope is `classify_idle`'s FINISHED/CUT/SILENT split layered on what KC-22 shipped, not the whole edge. KC-39 (queued) extends the same edge further, on top of whichever of this ticket and KC-22 is current when it lands: a continue whose diff does not change starts a fresh session instead of exhausting the budget in place.  
**Severity:** HIGH  
**File:** `tools/contest/runner.py`  
**Symbol:** `run_agent` (the `WAITING → HARVESTING` edge and the silence-`stall`/`timeout` → `STALLED` edge), `classify_idle`, `CONTINUE_PROMPT`, `IdleKind`  
**Round:** 48  
**Size:** S  
**Source:** the probe (`docs/kilo-contest/PROBE.md` §4) showed that a second `prompt_async` into an idle session continues it with full context — that is the mechanism. The failure it must cover: providers on free tiers cut a reply short (`finish_reason=length`, a 429 mid-stream, a tool loop that just stops). Two ways this surfaces: (a) Kilo emits `session.idle` exactly as after a finished turn — today the runner treats every idle as "the model is done", runs harvest, and a half-written change is sent back as a REWORK with reasons the model never needed; (b) no event arrives at all and the silence window (`idle_event_timeout_sec`) fires — today the runner kills the turn straight to STALLED (`no event for Ns`), losing the same in-flight work. Round 72 (2026-09-21) hit (b) live: `step-3-7-flash` and `hy3` were mid tool-calls, went 300 s silent, and were killed with no commit while `agnes-2-5-flash`/`mimo-v2-5` handed in. Both are the same "not finished, not wrong" turn and take the same `continue`.  
**Depends on:** KC-6.  
**Also touches:** `tools/contest/kilo_client.py` (nothing new; `tool_parts`, `messages`, `last_assistant_text` suffice), `tests/test_contest_runner.py`, `tests/_kilo_fake.py` (a turn may declare `"assistant": ""` and `"cut": true`)

---

## What happens today

`run_agent`: `WAITING` → `IdleResult.status == "idle"` → `HARVESTING`, with
no look at *how* the turn ended; and `WAITING` → silence (`stall`, or
`timeout` under `turn_timeout_sec`) → `STALLED` with the in-flight work
dropped. Neither край gives the cut turn another `continue`.

## What must change

1. **`classify_idle(client, session, turn_started_at) -> IdleKind`** —
   read the messages once and decide, in order:
   - `IdleKind.FINISHED` — the last assistant message has non-empty text
     **or** its last part is a `tool` part with `state.status == "completed"`
     and `PROGRESS.csv` was modified after `turn_started_at`;
   - `IdleKind.CUT` — the last assistant message has empty text and its
     last part is a `tool` part with `status == "running"|"pending"`, or the
     message `info.error` is set (`MessageOutputLengthError`, `APIError`),
     or `info.finish`/`finish_reason` (whichever this build carries —
     record it in the test double from a real capture) is `length`;
   - `IdleKind.SILENT` — empty text, no tool parts in this turn at all;
   - otherwise `FINISHED` (harvest decides).

2. **The edge.** `WAITING` → a turn that ended without handing in →
   `continue`, bounded by the existing `config.max_continues_per_attempt`
   (no new key). Two entries, one path:
   - idle fired → `classify_idle`; `FINISHED` → `HARVESTING` (unchanged),
     `CUT` or `SILENT` → the continue path below.
   - no idle fired and the silence window (`idle_event_timeout_sec`) fired
     under `turn_timeout_sec` (today's `no event for Ns` → STALLED) → the
     same continue path; the turn record keeps `idle_status: "stalled"` and
     names `idle_kind: SILENT`. The overall `turn_timeout_sec` край (`no
     idle after Ns`) is **not** a continue — that is a runaway turn, still
     STALLED.
   - continue path, when `run.continues < config.max_continues_per_attempt`:
     `run.continues += 1`, `client.prompt(session, CONTINUE_PROMPT)`, back to
     `WAITING` **without** touching `attempt` (a continue is not a rework).
     A silence continue waits `error_retry_backoff_sec` first (the session
     may be dead/rate-limited, unlike a clean idle) and, if the same silence
     край fires again with no new event, is not retried past the budget.
   - the budget spent → today's terminal (`HARVESTING` for an idle край,
     `STALLED` for a silence край); the turn record says
     `continues_exhausted`. A commit under the branch is harvested either way
     (KC-21).

3. **`CONTINUE_PROMPT`** — a module string: *"Your previous reply stopped
   before the task was finished. Continue exactly where you left off in
   this same worktree: finish the change, make sure it is one commit, then
   record it with `append_task.py`. Do not start over."* Sent as plain
   text; no ticket text repeated.

4. **Records.** `turns.jsonl` gains `idle_kind` and `continues` per turn;
   `AgentRun.continues` is serialised in `state.json`; `SUMMARY.md` (KC-7)
   gets a `cont.` column.

## Acceptance

- [ ] `tests/test_contest_runner.py`: a fake turn with `"assistant": ""`
      and a `tool` part `status: running` → `CUT`, the fake's request log
      shows a second `prompt_async` on the **same** session whose text is
      `CONTINUE_PROMPT`, `attempt` still 0; the fake's second turn finishes
      → `READY`, `continues == 1`; a turn with empty text and no tool parts
      → `SILENT`, same path; `max_continues = 1` with two cut turns → the
      third idle goes to harvest, turn record says `continues_exhausted`;
      a finished turn → no continue prompt sent.
- [ ] the silence край: a fake turn that emits no event until
      `idle_event_timeout_sec` (elapsed < `turn_timeout_sec`) → one
      `CONTINUE_PROMPT` on the **same** session, `attempt` still 0,
      `run.continues == 1`, turn record `idle_status: "stalled"`,
      `idle_kind: SILENT`; the same край again with the budget spent →
      STALLED, `continues_exhausted`; a commit on the branch under a spent
      silence край is harvested (KC-21). `no idle after turn_timeout_sec` →
      STALLED, no continue.
- [ ] `classify_idle` unit-tested on message fixtures copied from a real
      session export (`GET /session/{id}/message` of a probe run; commit
      the fixture under `tests/fixtures/kilo/`), including one with
      `info.error` set.
- [ ] `python3 -m pytest tests -n 4 -q --timeout=180 && python3 -m pytest tests_bugfix -n 4 -q --timeout=180` green.

## Out of scope

- Detecting a cut mid-stream by inspecting tokens (streaming heuristics) —
  the two signals this build gives reliably are `session.idle` and the
  `idle_event_timeout_sec` silence край, and both are covered above.
- Continuing after `session.error` — that is `ERROR`; the operator resumes.

## Ground rules (same as every round)

- Do not touch `CollectBridge._shrink`.
- No test starts a real `kilo` or calls a live provider.
- Do not edit `epic-tasks/`.
- One commit, no push; a test ships with the change and fails without it.
