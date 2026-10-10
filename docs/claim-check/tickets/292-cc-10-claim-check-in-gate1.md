# CC-10 — claim check in Gate 1: three voters with the code and a quote, instead of one reader

**Status:** DRAFT — a first sketch written before the live runs of CC-6 were finished. It will be
completed after the live run (numbers, the voters' roster, the quorum rule and the fallback below
are hypotheses until then). Do not copy it into `epic-tasks/` as it stands.
**Severity:** MEDIUM (it is the first place where the epic's tool feeds `--auto` directly)
**File:** `tools/auto/gate1_filter.py`
**Symbol:** `Gate1Filter._check_presence` (Stage B), a new `tools/auto/gate1_vote.py`
**Round:** —
**Size:** M
**Depends on:** CC-6 (voting with quotes, landed), CC-8 (one command, `truth.csv`)
**Also touches:** `contest.ini` / `agents.ini` (`[gate1]`), `tests/test_gate1_vote.py`, `docs/claim-check/RUNBOOK.md`

---

## Why

`main.py --auto "search and fix bugs"` plans with the Architect, and **Gate 1** keeps a task out of
the backlog when the bug it names is not there. Stage A (no LLM) checks the cited file and symbol exist;
Stage B (`_check_presence`) asks **one** model, shown the code block, for `confirmed`/`rejected`.
Our own audit of auto-generated "add error handling" lists found them almost all false positives
(memory: `improvements-md-autotask-review`), so Stage B is the line that matters, and today it rests on
one model's word with no quote and no second opinion.

CC-6 already does the harder version of the same job: several models of different families, each
answering with a verbatim quote from the evidence, the quote checked by code, accepted only when the
committed models agree. Live (round 280, run A, three voters): 57 of 80 fixture claims decided, 0 wrong,
32 of 50 code claims decided.

## What it does (sketch)

Stage B asks "is the problem described in this task present in this code?" That is a claim:
*"`<symbol>` in `<file>` has the problem: `<one-line instruction>`"*.

* `gate1_vote.presence_by_vote(candidate, view) -> (verdict, reason)` builds that claim, resolves its
  anchors against the candidate's own target at the pinned commit, builds the pack (CC-5) and runs
  `ask_with_packs` with the voters named in `[gate1] vote_profiles` (three families, placeholders in
  the ini).
* The verdict maps to the filter's existing outcomes: unanimous `TRUE` → `confirmed`; unanimous
  `FALSE` → `rejected` with the voters' quotes as the reason; anything else (`SPLIT`, `UNSURE`, fewer
  than three committed) → **the existing single-reader path**, unchanged (Stage B as today decides).
  So the vote can only *shortcut* a decision it is sure of; it never makes Gate 1 less strict than it is now.
* The vote's cost is reported next to the filter's own counters (`votes_asked`, `votes_decided`,
  `votes_overridden`), so the rate at which it decides is a number.
* `[gate1] claim_vote = off` (default) leaves Gate 1 byte for byte as it is.

## Tests (sketch)

`tests/test_gate1_vote.py`, fake voters, no network: unanimous TRUE confirms; unanimous FALSE rejects with
the quote; a split falls back to the single reader; a fabricated quote is downgraded and falls back; `off`
changes nothing (the existing Gate 1 tests stay green unchanged); a voter that raises is a result, not a crash.

## Acceptance (to be written after the live run)

* Offline: the fake-voter chain over the fixture's "task" claims.
* Live, the operator: a recorded set of Architect candidates with a known answer (true bug / false positive)
  through Gate 1 with the vote off and on: false positives let through, true bugs rejected, time per
  candidate. Numbers go to the round's `SUMMARY.md`.

## Open questions (to settle with the live numbers)

1. Which three voters, and the rate limit they can take: Gate 1 can ask hundreds of times a run (834 live
   Stage-B calls were counted once), each of them now three requests. Is it a shortcut for the candidates
   where the single reader and the vote disagree, or for all?
2. Is a unanimous `FALSE` enough to reject, or must the single reader agree (a rejected true bug costs more
   than a let-through false one)?
3. The pack's size for a task: the cited block alone, or with its callers and the git history?
4. Round-trip time: three voters in parallel are as slow as the slowest; a timeout falls back, never blocks.

5. **Only one model is ever available (the question that came with the draft).** Today
   `claim_vote.py` refuses: with fewer than `MIN_COMMITTED = 3` voters it prints "quorum is 3: no claim
   can be accepted". A **single-voter mode** (`[gate1] vote_quorum = 1`, never the default) would accept a
   verdict when that one model commits, its quote is verified by code, and it gives the same verdict in
   every one of N runs (the prompt varies per run). It is a weaker guarantee than three families, and the
   draft must say so in the output (`quorum=1`, which model). Live numbers from round 280 run A, each
   model alone, counting only votes that passed the quote check: hy3 76 right of 76 decided (fixture),
   21 of 21 (real); nemotron-3-ultra 70 of 70, 22 of 22; **deepseek-v4-flash 65 of 69 and 12 of 13**
   (4 + 1 wrong). So one good model with the quote check is already strong, and one bad one is not:
   the roster for a single-voter mode is chosen by measured wrong rate, not by availability. To be
   re-measured with `RUNS=3` before this ticket is cut.

## Not in scope

Changing Stage A, A0 or C; the Coder, executor or validators; the unanimity rule; Lenz.
