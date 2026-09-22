# KC-40 — Context near full with an existing diff: the model writes its own summary, and it is copied out, before anything is compacted or lost

**Status:** queued — after KC-10 and KC-39. Asked by the operator on 2026-09-22, as a step beyond KC-10: KC-10 compacts a session server-side (`POST /session/{id}/summarize`) once fill crosses `compact_at_percent`, which is the right answer on a clean tree, but a compact on a session that already holds an uncommitted diff can throw away the model's own account of *why* the diff looks the way it does, and KC-10 returns nothing to the caller either way — the session's internal history shrinks, but nothing leaves the session for the operator or a fresh session to read. This ticket covers the case KC-10 does not: fill is high **and** a diff already exists, so before anything is compacted away, the model is asked to write a short summary in its own words, and that text is copied out to a file. If it cannot produce one before the context is gone, the fallback is KC-39's fresh session, carrying whatever partial summary text exists alongside the raw diff.
**Severity:** HIGH (the failure mode is losing the only explanation of an in-progress diff, not just losing time)
**File:** `tools/contest/runner.py`, `tools/contest/kilo_client.py`
**Symbol:** `runner.maybe_summarize_before_compact` (new), `runner.context_fill`/`KiloClient.model_limit` (KC-10, reused), `_diff_signature`/the session-reset path (KC-39, reused)
**Round:** 79
**Size:** L
**Size note:** larger than KC-10 or KC-39 individually because it composes both of them and because its Acceptance below requires a live-model result, not just a green fake suite — whether a real, nearly-full-context free-tier model can still produce a *useful* summary is an empirical question this ticket has to answer, not just wire up.
**Source:** paraphrased from the operator: "if the model has written a lot and there's a diff between the original and the current state, and the continuation keeps writing, try to get it to produce a summary, then extract that summary at the end if it succeeds; if it doesn't, start a new round that says there is uncommitted data and it should be used to finish this ticket." The operator separately noted this needs a real test — printing some large repeated output (their example: the letter `a` a thousand times) against a small budget (their example: 100) to force the edge open, because whether the summary is any good can only be judged against a live model.
**Depends on:** KC-10 (queued — `context_fill`, `compact_at_percent`, `model_limit`/`session_tokens`), KC-39 (queued — `_diff_signature`, the live session-reset path this ticket's fallback reuses).
**Also touches:** `tests/test_contest_runner.py`, `tests/_kilo_fake.py` (a scenario needs a scripted assistant reply to the summary prompt, plus KC-10's token/`/provider` shapes), `contest.ini` (`summary_at_percent`, new), `contest-bench/kc40/` (new — the live probe below)

---

## What happens today

Nothing reads fill at all — `context_fill` does not exist until KC-10
lands. Once KC-10 does land, its `compact` calls the server's own
`/session/{id}/summarize` and waits for `session.compacted` — a session-
internal operation that returns no text to the caller. A model sitting on
a half-finished diff at high fill either gets compacted (and whatever
reasoning explained the non-obvious parts of the diff may not survive
that) or overflows (`ContextOverflowError`, one retry per KC-10 §6, then
`ERROR`) — either way, nobody outside the session ever sees the model's
own account of what it was doing, and a human (or a fresh KC-39 session)
reading the diff afterwards has only the code, no notes.

## What must change

1. **The edge**, checked after `context_fill` (KC-10) and before
   `maybe_compact`, so a summary is attempted before a plain compact when
   both would fire: `fill >= config.summary_at_percent / 100` **and** the
   worktree already has an uncommitted diff (`_dirty_tree(ws)` non-empty,
   `_commits_above(ws) == 0` — the same gate KC-22/KC-39 use) **and** this
   has not already been tried once this `run.attempt` (a
   `run.summary_attempted_at_attempt` flag, cleared on rework exactly like
   `continue_used`).
2. **`SUMMARY_PROMPT`** — a module string sent as the next prompt in the
   same session: *"Your context is nearly full and nothing is committed
   yet. Before anything else, write a short summary here in the chat of
   exactly what you changed, why, and what is left to do. Do not modify
   any files in this reply."* Wait for `session.idle` as for any other
   prompt.
3. **Copying it out.** `client.last_assistant_text(session)` after that
   idle: non-empty → write it to `<agent>.summary.md` under `out_dir` and
   set `run.summary` (serialised in `state.json`, shown in `SUMMARY.md`,
   KC-7) — this is the "copy the summary" the operator asked for: the text
   leaves the session into a file that survives whatever the server does
   to the session's internal history next, and survives a KC-39 session
   reset or a `--resume`, unlike anything kept only in the old session.
4. **Then proceed as KC-10 would have.** If fill is still over
   `compact_at_percent` after the summary turn, compact now; the
   interrupted turn's own continue/rework logic resumes on the next
   prompt, which is appended with one line ("your summary above still
   applies") instead of repeating the ticket.
5. **The fallback, when the summary attempt itself fails** — an empty
   reply, or the summary turn ends in `session.error` or the silence
   window: treat it exactly as KC-39's no-progress edge (reuse it, do not
   duplicate it) — abort, fresh session, `round_prompt(..., dirty=
   _dirty_tree(ws))`, with one addition: if the failed attempt produced any
   partial text before failing, prepend it to that prompt as "a previous
   attempt on this ticket left this note before running out of context:
   …". This is the operator's fallback: *"start a new round with the words
   'there is uncommitted data, use it to finish this ticket'"* —
   implemented as KC-39's existing mechanism, not a second one.
6. **Carrying `<agent>.summary.md` forward.** A second or third session in
   the same attempt (via KC-39 resets, or a `--resume`'s `_plan`) appends
   to the same file rather than overwriting it, so the operator reading it
   later sees the whole chain, not just the last attempt.
7. **Records.** `turns.jsonl` gains `summary_attempted` and
   `summary_captured` booleans on the triggering turn.

## Acceptance

- [ ] `tests/test_contest_runner.py` (fake): fill at/above
      `summary_at_percent` with a dirty tree, first time this attempt →
      the request log shows one extra `prompt_async` whose text is
      `SUMMARY_PROMPT` before any `summarize` call; `<agent>.summary.md`
      exists afterwards with the fake's scripted reply; a **clean** tree at
      the same fill → no summary prompt, KC-10's compact path runs
      unmodified; a second crossing of the threshold in the same
      `run.attempt` → not asked again.
- [ ] a fake turn where the summary prompt gets an empty reply, and one
      where it ends in `session.error` → both show KC-39's `abort` +
      second `POST /session` path firing, with the new session's opening
      prompt containing the `git status` lines.
- [ ] a fake scenario where a non-empty summary is captured, then a later
      KC-39 reset happens in the same attempt → the new session's opening
      prompt contains the previously captured summary text, not just the
      file names.
- [ ] `<agent>.summary.md` accumulates across two sessions of the same
      attempt in the fake, rather than the second overwriting the first.
- [ ] **Real test on live models, required — this is a feasibility
      question a fake cannot answer.** The fake only proves the request
      ordering; whether a real, nearly-full-context free-tier model can
      still produce a *coherent and truthful* summary of its own unfinished
      diff is an empirical result, not a wiring check. Build
      `contest-bench/kc40/live_probe.py`, `contest-bench/kc6/
      live_smoke.py`-style: a ticket that (a) makes one small, real file
      edit first, so a genuine uncommitted diff exists, then (b) asks the
      model to print a large repeated token (the operator's own example:
      the letter `a`, about 1000 times) to burn context fast. Set
      `context_limit_fallback`/`compact_at_percent`/`summary_at_percent`
      low enough (the operator's own example: a budget on the order of
      100 tokens) that the edge trips in minutes, not by waiting on a real
      32k window. Run it against at least two live models from the roster
      and, for each, record by hand in `contest-bench/kc40/RESULTS.md`:
      whether a summary was captured at all; whether it is a truthful,
      useful account of the real diff (read both side by side and say so
      in the write-up — a summary that is fluent but unrelated to the
      actual diff is not a pass); and, for whichever model (if any) failed
      to produce one, what the fallback session actually did with the
      partial text. A plumbing success with a useless or hallucinated
      summary is not a passing result for this ticket.
- [ ] `python3 -m pytest tests -n 4 -q --timeout=180 && python3 -m pytest tests_bugfix -n 4 -q --timeout=180` green.

## Out of scope

- Summarizing on a clean tree — KC-10's plain compact already covers that
  case and nothing here changes it.
- Any automatic judgement of summary quality inside the suite — the live
  test's judgement is a human reading the text next to the diff by hand,
  written into `RESULTS.md`, not a check `pytest` can assert.
- Deciding the exact relationship between `summary_at_percent` and
  `compact_at_percent` beyond "this fires first when both would" — a
  default ordering is suggested (`summary_at_percent` higher, e.g. `90`
  against KC-10's suggested `compact_at_percent = 80`) but the implementer
  picks and tests the final defaults.

## Ground rules (same as every round)

- Do not touch `CollectBridge._shrink`.
- No test starts a real `kilo` or calls a live provider — `contest-bench/`
  is not `tests/` and is run by hand, exactly like `contest-bench/kc6/
  live_smoke.py`.
- Do not edit `epic-tasks/` (other than this round's own ticket file).
- One commit, no push; a test ships with the change and fails without it.
