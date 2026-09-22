# KC-39 — A continue whose diff repeats the previous one starts a fresh session, not a same-session harvest

**Status:** queued — after KC-38, last in the queue; asked by the operator on 2026-09-22, not from a live round: KC-22's continue mechanism grants the full `max_continues_per_attempt` budget to any turn that still shows a dirty tree, with no check that the dirty tree is any *different* from the one at the last continue. Sequenced with KC-9 (queued, same edge — `classify_idle`'s FINISHED/CUT/SILENT split decides whether a continue is sent at all; this ticket decides, once one is being sent for the third-plus time, whether it goes to the same session or a new one). Whichever of KC-9 and KC-39 lands second rebases onto the first.
**Severity:** MEDIUM (no work is lost — a commit under the branch is still harvested either way, KC-21 — but a model that repeats itself burns the whole continue budget for nothing and then gets a `HARVESTING`/`REWORK` critique inside the very session that produced the loop, which is the session least likely to break out of it)
**File:** `tools/contest/runner.py` (`run_agent` — the `idle.status == "idle"` branch, KC-22's `continue_used`/`budget` block), `contest.ini`
**Symbol:** `run_agent`, `continue_message`, `_dirty_tree`, `_diff_signature` (new), `ContestConfig.max_sessions_per_attempt` (new)
**Round:** 78
**Size:** M
**Source:** paraphrased from the operator: "extend the continue criterion — right now three continues are granted by default (`max_continues_per_attempt` in `contest.ini`); if the third one still adds nothing, a new session should start instead." Confirmed against the code: `run_agent`'s continue branch (`tools/contest/runner.py`, the `idle.status == "idle"` case) reads `_dirty_tree(ws)` — `git status --porcelain` file names and status letters only, never the diff's content — and treats any non-empty result as "still working", with no comparison to the previous continue's tree. `max_continues_per_attempt` is 2 in `contest.ini` today (three prompts total counting the initial one), not three; the operator's "three" is presumably a local override and is not itself a bug.
**Depends on:** KC-22 (landed `286cff9` — the continue loop, `continue_message`, `round_prompt(dirty=...)` this ticket extends).
**Also touches:** `tests/test_contest_runner.py`, `tests/_kilo_fake.py`, `contest-bench/kc39/` (new — the live probe below)

---

## What happens today

`run_agent`'s `idle.status == "idle"` branch (KC-22): while
`continue_used < max_continues_per_attempt` and `_commits_above(ws) == 0`,
a non-empty `_dirty_tree(ws)` is enough to grant another continue —
`continue_message(dirty)` is sent, `continue_used += 1`, back to `WAITING`.
`_dirty_tree` returns `git status --porcelain` lines: which files are
modified, not what changed inside them. Two continues in a row where the
model touches the same file with different half-finished content, or
writes the same "let me check…" text and nothing else, look identical to
this check — both are "the tree is dirty" — so both are granted. When the
budget is spent, the loop falls through to `error = state = None` and
proceeds straight to `HARVESTING` **in the same session** (the same
context that already produced N turns of no progress), which then either
finds a `READY` diff by luck or sends a `REWORK` the model has already
shown it cannot act on, spending one of `max_rework`'s attempts on it.

## What must change

1. **`_diff_signature(ws) -> str`** — a signature of the *content* of the
   uncommitted work, not just the file list: `git add -A -N` (intent to
   add, so new untracked files appear in the diff text) followed by
   `git diff --no-color -- ':!runs'`, hashed (e.g. `sha256`). Empty tree →
   `""`. This is the piece `_dirty_tree` does not give today — it exists
   to make "the same diff again" detectable, which file names alone
   cannot do.
2. **Recording it.** Each time the continue branch is about to grant a
   continue, compute `_diff_signature(ws)` and compare it with the
   signature recorded at the *previous* continue of this attempt (a new
   `run.last_diff_signature` field; cleared on rework and on a session
   reset, same as `continue_used`). The first continue of an attempt is
   always granted when the tree is dirty, exactly as today — there is
   nothing yet to compare it against.
3. **The no-progress edge.** When the continue about to be granted would
   be the **last** one under `max_continues_per_attempt` *and* its
   signature equals the previous continue's: instead of falling through to
   `HARVESTING` in the same session, do what `_plan`'s `--resume` path
   already does for a dirty worktree after a process restart, live, inside
   `run_agent`:
   - `client.abort(session)`, then `client.create_session(...)` with the
     same `provider_id`/`model_id`/rules/title;
   - `run.session_id` = the new session's id; `continue_used = 0`;
     `run.last_diff_signature = ""`; `run.attempt` **unchanged** — a fresh
     session is not a rework;
   - the next prompt is `round_prompt(spec.name, ticket_path, ws.base_sha,
     dirty=_dirty_tree(ws))` — the identical "your worktree still holds
     this uncommitted work, finish it here, do not start over" paragraph
     KC-22 already sends after a `--resume`, just triggered live instead of
     only after an operator restart.
4. **A ceiling.** New `contest.ini` key `max_sessions_per_attempt`
   (suggested default `2`): the reset in point 3 only fires while the
   number of sessions already created for this attempt is below it; once
   spent, fall through to today's same-session `HARVESTING`, so this
   mechanism cannot turn a bad model into an unbounded chain of sessions.
   `0` disables the whole ticket — the tree behaves exactly as it does
   today.
5. **Cost and the transcript stop being single-session.** `_record_session`
   is called once per replaced session (right before its `abort`), not
   only at the very end of `run_agent`; `run.cost`/`run.tokens` become the
   sum across every session of the attempt, and the per-agent transcript
   file gains one entry per session (do not overwrite the first session's
   `<agent>.session.json` with the second's).
6. **Records.** `turns.jsonl` gains `diff_signature` on every continue
   turn and `new_session: "<id>"` on the turn that triggers a reset;
   `state.json`/`SUMMARY.md` (KC-7) show the number of sessions used per
   agent.

## Acceptance

- [ ] `_diff_signature` unit test: two worktrees with the same file list
      but different file content hash differently; adding one untracked
      file changes the signature; a clean tree → `""`.
- [ ] `tests/test_contest_runner.py` (fake): a model whose every turn
      writes byte-identical content to the same file, with
      `max_continues_per_attempt = 2` and `max_sessions_per_attempt = 2` —
      after the second continue's signature matches the first's, the
      fake's request log shows `abort` on the first session, then a second
      `POST /session` with the same `provider_id`/`model_id`, then a
      `prompt_async` on the new session whose text contains the `git
      status` lines; `continue_used` is 0 on the new session; `run.attempt`
      unchanged; the fake's second session finishing cleanly → `READY`.
- [ ] the same fixture with `max_sessions_per_attempt = 1` when a reset has
      already happened once this attempt → no second reset: the next
      exhaustion falls through to same-session `HARVESTING`, as today.
- [ ] a model whose continues each add genuinely new content (changing
      signature every time) → no reset is ever triggered; budget
      exhaustion still goes to same-session `HARVESTING` exactly as before
      this ticket (regression guard on the unchanged path).
- [ ] `run.cost`/`run.tokens` after one reset equal the sum of both
      sessions' `GET /session/{id}` numbers in the fake; the transcript
      file holds both sessions' messages, not just the second's.
- [ ] `max_sessions_per_attempt = 0` → identical behaviour to the tree
      before this ticket (regression guard).
- [ ] **Real test on live models, required — the fake only proves the
      wiring.** Whether a genuinely stuck free-tier model produces an
      unchanging diff for real, and whether the reset actually unsticks it
      or just moves the loop to a second session, is not something a
      scripted fake can answer. Build `contest-bench/kc39/live_probe.py`
      in `contest-bench/kc6/live_smoke.py`'s own style: a ticket that asks
      the model to reply with nothing but the letter `a` printed 1000
      times and record it with `append_task.py` — cheap, deterministic,
      and gives the model nothing to converge on, so the diff (if any) is
      either empty or unchanging turn to turn. Run it with the repo's
      `max_continues_per_attempt` and `max_sessions_per_attempt = 1`
      against at least two live models from the roster and confirm by
      hand: (a) a real `abort` and a real second `POST /session` happen
      against the live server, not just the fake; (b) `state.json` shows
      two `session_id`s for the agent that triggered it; (c) the round
      still terminates inside `2 × turn_timeout_sec` rather than hanging.
      Record the run the way `contest-bench/kc6/RUNBOOK.md` §10 records
      its live rounds — commands, the table of what happened, raw
      `state.json`/`turns.jsonl` under `contest-bench/kc39/`. A green fake
      suite alone does not close this ticket.
- [ ] `python3 -m pytest tests -n 4 -q --timeout=180 && python3 -m pytest tests_bugfix -n 4 -q --timeout=180` green.

## Out of scope

- Any change to what makes the *first* continue of an attempt fire — still
  just "the tree is dirty", exactly as KC-22 left it.
- Detecting "no progress" any other way than the worktree's own diff
  content — token counts, elapsed time, tool-call counts are KC-36's
  concern (a different edge, inside one turn) and are not reused here.
- Writing or capturing a summary of the stuck work before the reset — the
  reset's prompt carries only the raw `git status` lines, same as KC-22's
  `--resume` path. A model-authored summary is KC-40, which depends on
  this ticket's reset mechanism as its own fallback.

## Ground rules (same as every round)

- Do not touch `CollectBridge._shrink`.
- No test starts a real `kilo` or calls a live provider — `contest-bench/`
  is not `tests/` and is run by hand, exactly like `contest-bench/kc6/
  live_smoke.py`.
- Do not edit `epic-tasks/` (other than this round's own ticket file).
- One commit, no push; a test ships with the change and fails without it.
