# KC-43 — A ticket too large for one turn runs as numbered legs, with a leg record handed from each to the next

**Status:** queued — last, after KC-36, KC-39, KC-40, KC-41 and KC-42, every one of which it composes. Asked by the operator on 2026-09-22 as the frame the other tickets are parts of.
**Severity:** MEDIUM (nothing is lost today that KC-41 does not already save; this ticket is about tickets that *cannot* be finished in one turn at all, which the epic has so far avoided by keeping tickets small)
**File:** `tools/contest/cli.py` (`cmd_run`, `_parser`), `tools/contest/runner.py` (`run_round`), `tools/contest/workspace.py` (`prepare_round` — the "never reuse a previous round's worktree" rule gains one exception), `contest.ini`
**Symbol:** `cmd_run`, `run_round`, `run_leg` (new), `leg_record` (new), `RoundState.leg` (new), `prepare_round(..., carry_from=...)` (new)
**Round:** 82
**Size:** L
**Source:** the operator, 2026-09-22, describing what they already do by hand: *"сейчас я её делаю вручную — там на третьем ходе уже новый начинаю чат и заставляю модель продолжать работать с начатыми (изменёнными) файлами"*. The manual procedure is: let the model work until its context or the clock gives out, open a **new** session against the **same** worktree, and tell it to continue from the files it has already changed. It works; it is untooled. Round 64 is the supporting evidence from the other direction — four of five working agents were cut mid-flight, two of them a single failing test away from done (`step-3-7-flash`: `tests:1✗`; `hy3`: `tests:3✗`), and a second leg would plainly have closed both.
**Depends on:** KC-36 (queued — a leg cut by wall clock rather than by idleness multiplies round 64's failure by the number of legs), KC-41 (queued — every leg must end in a captured commit or there is nothing to hand over), KC-40 (queued — the model-written summary is one field of the leg record), KC-39 (queued — the live session-reset path a new leg reuses), KC-42 (queued — a leg must not be spent by an agent that never starts).
**Also touches:** `tests/test_contest_cli.py`, `tests/test_contest_runner.py`, `tests/_kilo_fake.py`, `docs/collect-epics/RUN-THE-EPIC-COMPETITION.md`, `contest-bench/kc43/`

---

## What happens today

A round is one shot. `cmd_run` prepares a worktree per agent at the base,
`run_round` drives one turn (plus KC-22's continues and up to `max_rework`
rework attempts *inside the same session*), harvests, exports, and the
round is over. `prepare_round` refuses outright to reuse a previous
round's worktree, by design (`workspace.py`'s docstring: "a worktree must
never be carried across rounds").

So the only mechanisms for "keep going" are (a) a continue, which stays in
the same session and therefore in the same context that is running out,
and (b) a rework, which is a *critique* of finished work, not a
continuation of unfinished work. Neither is "a fresh mind, the same code",
which is what the operator does by hand and what a large ticket needs.

## What must change

1. **A leg.** `run_leg(...)` is today's `run_round` body. A round is a
   sequence of legs, numbered `<round>.<leg>` (`65.1`, `65.2`, `65.3`) in
   `state.json`, in `out_dir` (`contest-out/65.2/`) and in every log line.
   `--legs N` on `run`; `legs = 1` in `contest.ini` keeps today's shape as
   the default.
2. **The worktree is carried, the session is not.** Leg *n+1* runs in leg
   *n*'s worktree on leg *n*'s branch — `prepare_round(..., carry_from=…)`,
   the one sanctioned exception to the no-reuse rule, guarded so it can
   only ever carry from the immediately preceding leg of the same round and
   the same agent. The session is always new. This is the whole point: the
   code survives, the exhausted context does not.
3. **The leg record** — `leg_record(run, ws, out_dir) -> Path`, written at
   the end of every leg, **derived from git and the logs, not from prose**:
   - the files touched, with a diffstat;
   - the commit the leg ended on (KC-41's deadline commit or the model's);
   - which pytest roots ran and what they returned;
   - the harvest verdict and its reason codes, when one ran;
   - the agent's own last message, quoted, as *one field among many*;
   - KC-40's model-written summary when it exists;
   - an explicit "what is left" list.

   Only the last two fields can come from a model. Everything above them is
   mechanical, and a leg record whose mechanical fields are empty says so
   rather than letting prose fill the gap. Hard constraint: the record must
   be readable in under a minute — if it is longer than the diff it
   describes, it is wrong.
4. **The next leg's prompt** is the ticket, the leg records so far (newest
   first — they are short by construction), and an explicit instruction to
   **continue**: the worktree already holds work, it is yours, do not start
   over.
5. **Scoring happens once.** Intermediate legs are not judged against the
   bench and do not export patches; they only have to end in a captured
   state. The last leg harvests and exports exactly as a round does today.
6. **A leg that finishes early ends the relay** for that agent: a `READY`
   harvest on leg 2 of 3 means leg 3 is not run for it. The round still
   waits for the other agents' legs.

## Acceptance

- [ ] `tests/test_contest_cli.py`: `--legs 3` produces `contest-out/65.1`,
      `65.2`, `65.3`; each leg's `state.json` names its leg number; the
      worktree path is identical across all three and the `session_id`
      differs in each.
- [ ] `tests/test_contest_runner.py`: leg 2's prompt contains leg 1's
      record and the word "continue"; it does **not** contain a rework
      critique (a relay is not a rework, and `run.attempt` is unchanged
      across legs).
- [ ] A leg-2 `READY` stops that agent's relay; a leg-2 `REWORK` does not.
- [ ] `leg_record` on a leg that produced nothing at all writes a record
      whose mechanical fields are explicitly empty, and the next leg's
      prompt still names the ticket's declared files.
- [ ] `prepare_round(carry_from=…)` refuses to carry from anything but the
      immediately preceding leg of the same round and agent — a previous
      *round's* worktree is still refused exactly as today.
- [ ] `legs = 1` → byte-identical behaviour to the tree before this ticket,
      including the `contest-out/<NN>/` path with no leg suffix
      (regression guard).
- [ ] **Live round, required.** A relay cannot be validated by a fake: the
      question it exists to answer is whether a fresh session with a leg
      record in hand actually *continues* the work rather than rewriting
      it, and only a real model can answer that. Run one live 2-leg round
      on a ticket known to be too big for one turn, against at least three
      roster models, and record in `contest-bench/kc43/RESULTS.md`: per
      agent, the leg-1 and leg-2 diffstats, how much of leg 1's diff
      survived into leg 2 (the number that decides whether this ticket
      works), and the final verdicts. A relay in which leg 2 routinely
      discards leg 1 is a failed design, not a passing test — say so in
      RESULTS.md if that is what happens.
- [ ] `python3 -m pytest tests -n 4 -q --timeout=180 && python3 -m pytest tests_bugfix -n 4 -q --timeout=180` green (sequentially).

## Out of scope

- **Cross-agent relay** — leg *n+1* of one agent starting from another
  model's leg-*n* entry. It is the most interesting idea in this area and
  the most destructive: it removes the independence that makes a round a
  comparison at all, and "who won" stops meaning anything. If it is ever
  built it is a separate *mode* — "finish this" rather than "compete" —
  and a separate ticket, after this one has run live at least twice.
- Merging the relay with the existing `rework` path. They are two different
  answers to "try again" and probably should become one, but not in the
  ticket that introduces the second one.
- Choosing the number of legs automatically — KC-44.
- Any change to what a leg's *turn* does internally. A leg is today's
  round body, unmodified.

## Ground rules (same as every round)

- Do not touch `CollectBridge._shrink`.
- No test starts a real `kilo` or calls a live provider — `contest-bench/`
  is not `tests/` and is run by hand.
- Do not edit `epic-tasks/` (other than this round's own ticket file).
- One commit, no push; a test ships with the change and fails without it.
