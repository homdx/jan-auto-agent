# KC-9 — A session that went idle without finishing is nudged with `continue` — same session, bounded

**Status:** queued — after KC-6 (round 45); lands before KC-7 is run live. Written against `f069f07`.  
**Severity:** HIGH  
**File:** `tools/contest/runner.py`  
**Symbol:** `run_agent` (the `WAITING → HARVESTING` edge), `classify_idle`, `CONTINUE_PROMPT`, `IdleKind`  
**Round:** 48  
**Size:** S  
**Source:** the probe (`docs/kilo-contest/PROBE.md` §4) showed that a second `prompt_async` into an idle session continues it with full context — that is the mechanism. The failure it must cover: providers on free tiers cut a reply short (`finish_reason=length`, a 429 mid-stream, a tool loop that just stops); Kilo then emits `session.idle` exactly as after a finished turn. Today the runner treats every idle as "the model is done", runs harvest, and a half-written change is sent back as a REWORK with reasons the model never needed — it was never finished, not wrong.  
**Depends on:** KC-6.  
**Also touches:** `tools/contest/kilo_client.py` (nothing new; `tool_parts`, `messages`, `last_assistant_text` suffice), `tests/test_contest_runner.py`, `tests/_kilo_fake.py` (a turn may declare `"assistant": ""` and `"cut": true`)

---

## What happens today

`run_agent`: `WAITING` → `IdleResult.status == "idle"` → `HARVESTING`.
No look at *how* the turn ended.

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

2. **The edge.** `WAITING` → idle → `classify_idle`:
   - `FINISHED` → `HARVESTING` (unchanged);
   - `CUT` or `SILENT` and `run.continues < config.max_continues`
     (new `[contest]` key, default 3) → `run.continues += 1`,
     `client.prompt(session, CONTINUE_PROMPT)`, back to `WAITING` **without**
     touching `attempt` (a continue is not a rework);
   - the budget spent → `HARVESTING` anyway (harvest will say REWORK or
     READY on what is there; the turn record says `continues_exhausted`).

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
- [ ] `classify_idle` unit-tested on message fixtures copied from a real
      session export (`GET /session/{id}/message` of a probe run; commit
      the fixture under `tests/fixtures/kilo/`), including one with
      `info.error` set.
- [ ] `python3 -m pytest tests -q --timeout=180 && python3 -m pytest tests_bugfix -q --timeout=180` green.

## Out of scope

- Detecting a cut *before* idle (streaming heuristics) — idle is the
  only signal this build gives reliably.
- Continuing after `session.error` — that is `ERROR`; the operator resumes.

## Ground rules (same as every round)

- Do not touch `CollectBridge._shrink`.
- No test starts a real `kilo` or calls a live provider.
- Do not edit `epic-tasks/`.
- One commit, no push; a test ships with the change and fails without it.
