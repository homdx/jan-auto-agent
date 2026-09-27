# KC-40 — Context near full with an existing diff: the model writes its own summary, and it is copied out, before anything is compacted or lost

> ## Ticket audit — 2026-09-27 (after the KC-10 patch series; branch head `b5257cf`)
> **Verdict: still needed. KC-10 is now landed as `5f439e3`; the deliberate, diff-aware summary and dedicated summary file remain unimplemented. The adjacent context tracking and Kilo-generated compact summary are already present through KC-67/KC-69/KC-10 and must be reused.**
>
> | Checked against real code | Found |
> |---|---|
> | `runner.maybe_summarize_before_compact`, `SUMMARY_PROMPT`, `<agent>.summary.md` file, `summary_at_percent`, `summary_attempted`/`summary_captured` fields | **None exist.** Zero hits repo-wide. This ticket's actual net-new deliverable (a *deliberate, diff-aware* prompt asking the model to explain an uncommitted diff, persisted to its own file) is genuinely unbuilt. |
> | ⚠️ **"Nothing reads fill at all — `context_fill` does not exist until KC-10 lands"** | **Stale.** KC-67/KC-69 established remembered sizing and between-prompt compaction; KC-10 (`5f439e3`) now adds the client token/limit calls and the configured fallback. The working path is `_context_budget`, `_context_tokens`, `_context_gate`, and `_compact_session`, driven by `context_memory.compact_at_percent(config)` (`contest.ini`, default 80%). KC-10's proposed symbol names `context_fill` and `maybe_compact` remain absent, but their behavior exists under the current interfaces. |
> | ⚠️ **"nobody outside the session ever sees the model's own account of what it was doing"** | **Partially false.** KC-69's `swap_session()` reads Kilo's server-generated compact summary with `_summary_text()` and forwards it to the fresh session. The text is also recorded in turn/state output. What is missing is a deliberate prompt about the uncommitted diff and a dedicated cumulative `<agent>.summary.md` file. |
> | `_dirty_tree`, `_commits_above` (the diff-exists gate this ticket reuses) | **Already exist** (`runner.py`, `runner.py`). Correctly used without claiming novelty. |
> | `contest-bench/kc40/` | **Does not exist.** No live probe started. |
> | **`KC-10` dependency** | **Landed `5f439e3`** (INDEX row 49); its live provider limit, session token, compact, and fallback paths are available. |
> | **`KC-39` dependency** | Still accurate — confirmed `queued` (`INDEX.md` row 78), `_diff_signature` absent from the repo. |
>
> **Net effect:** KC-10 is complete; KC-39 (INDEX row 78) is the only remaining dependency. The ticket remains queued because the targeted summary and dedicated file are absent. Its context and summary narrative is corrected below to avoid duplicating the landed KC-67/KC-69/KC-10 machinery.

**Status:** queued — KC-10 landed `5f439e3`; KC-39 (INDEX row 78) remains the blocking dependency. Asked by the operator on 2026-09-22: when context is high and an uncommitted diff exists, explicitly ask the model what it changed, why, and what remains, then save that reply in a dedicated file before compacting. KC-10 compacts at the configured threshold; KC-67/KC-69 also carry Kilo's server-generated summary into a replacement session and record it in turn/state output. Neither mechanism deliberately explains the current diff or writes a cumulative `<agent>.summary.md`. KC-39's fresh-session path is still the intended fallback, with the raw diff and any partial summary carried forward.
**Severity:** HIGH (the failure mode is losing the only explanation of an in-progress diff, not just losing time)
**File:** `tools/contest/runner.py`, `tools/contest/kilo_client.py`
**Symbol:** `runner.maybe_summarize_before_compact` (new — confirmed absent from the repo), `runner.context_fill`/`KiloClient.model_limit` ~~(KC-10, reused)~~ **— these exact names still don't exist; the equivalent, already-landed functions to build on are `_context_gate`/`_context_tokens`/`_context_budget` (`runner.py`) and `context_memory.compact_at_percent()` — corrected 2026-09-27**, `_diff_signature`/the session-reset path (KC-39, reused — confirmed `_diff_signature` still absent; `_dirty_tree`/`_commits_above` already exist and are the closer fit for "reused")
**Round:** 79
**Size:** L
**Size note:** larger than KC-10 or KC-39 individually because it composes both of them and because its Acceptance below requires a live-model result, not just a green fake suite — whether a real, nearly-full-context free-tier model can still produce a *useful* summary is an empirical question this ticket has to answer, not just wire up.
**Source:** paraphrased from the operator: "if the model has written a lot and there's a diff between the original and the current state, and the continuation keeps writing, try to get it to produce a summary, then extract that summary at the end if it succeeds; if it doesn't, start a new round that says there is uncommitted data and it should be used to finish this ticket." The operator separately noted this needs a real test — printing some large repeated output (their example: the letter `a` a thousand times) against a small budget (their example: 100) to force the edge open, because whether the summary is any good can only be judged against a live model.
**Depends on:** KC-10 (landed `5f439e3` — context sizing and compact path), KC-39 (queued — `_diff_signature` and the fresh-session reset this ticket's fallback reuses).
**Also touches:** `tests/test_contest_runner.py`, `tests/_kilo_fake.py` (a scenario needs a scripted assistant reply to the summary prompt, plus KC-10's token/`/provider` shapes), `contest.ini` (`summary_at_percent`, new), `contest-bench/kc40/` (new — the live probe below)

---

## What happens today

**[Corrected 2026-09-27 — the paragraph below described the tree before KC-67/KC-69 landed; it is now out of date. Original wording is preserved struck through, current reality follows.]**

~~Nothing reads fill at all — `context_fill` does not exist until KC-10
lands. Once KC-10 does land, its `compact` calls the server's own
`/session/{id}/summarize` and waits for `session.compacted` — a session-
internal operation that returns no text to the caller. A model sitting on
a half-finished diff at high fill either gets compacted (and whatever
reasoning explained the non-obvious parts of the diff may not survive
that) or overflows (`ContextOverflowError`, one retry per KC-10 §6, then
`ERROR`) — either way, nobody outside the session ever sees the model's
own account of what it was doing, and a human (or a fresh KC-39 session)
reading the diff afterwards has only the code, no notes.~~

**Current reality:** fill is read and compaction fires automatically. KC-67/KC-69 provide remembered sizing and recovery; KC-10 (`5f439e3`) adds Kilo's live model limit, session token calls, compact endpoint, and fallback sizing. `_context_gate` checks fill against `context_memory.compact_at_percent(config)` (default 80%, `contest.ini`) before a continue/rework prompt and calls `_compact_session`. If compacting does not free enough context, `swap_session` opens a fresh session and forwards any Kilo-generated compact summary through `CONTEXT_CONTINUE_NOTE`; that text is also recorded in turn/state output.

**What is still genuinely missing — this ticket's real, undiminished job:**
1. None of the above is gated on "a diff already exists uncommitted." It fires on fill alone; a clean tree and a dirty tree are compacted the same way today.
2. `_summary_text` only recovers *Kilo's own server-generated* compaction summary (whatever the server chose to keep when asked to shrink history) — nobody explicitly asks the model "what did you change, why, what's left," which is what actually explains a hand-written diff.
3. The generated summary can survive in `turns.jsonl`/`state.json` and reach a fresh session, but there is no dedicated, cumulative `<agent>.summary.md` file for the operator or later tooling.
4. Kilo's compact summary may preserve useful context, but it is not a deliberate, diff-aware account of the changes. The targeted prompt and file persistence remain unbuilt; KC-10/KC-67/KC-69 supply the surrounding machinery.

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
