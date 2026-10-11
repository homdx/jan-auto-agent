# CC-10 — claim check in Gate 1: three voters with the code and a quote, instead of one reader

**Status:** FINAL, open — CC-8 (290) has landed, so it can be cut into `epic-tasks/`; the numbers below are from live runs of rounds 280 and 285 plus the sensenova run of round 290's session. The one thing the code cannot give is the recorded Architect candidates: the operator step in Acceptance records them.
**Severity:** MEDIUM (it is the first place where the epic's tool feeds `--auto` directly)
**File:** `tools/auto/gate1_filter.py`
**Symbol:** `Gate1Filter._check_presence` (Stage B), a new `tools/auto/gate1_vote.py`
**Round:** —
**Size:** M
**Depends on:** CC-6 (voting with quotes, landed), CC-7 (before/after, landed), CC-8 (290: one command, `truth.csv`, `pending.md` — the operator step records its Architect candidates through it, so CC-8 lands first)
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

## Acceptance (two layers)

**Layer 1 — offline, in the round (no network, no key).**
* `tests/test_gate1_vote.py` as in Tests above, with fake voters that answer in the **real reply shapes** (a quote over
  two lines, separate JSON objects, a dead voter) — not only a clean JSON.
* The fake-voter chain over the fixture (`contest-bench/cc/claims_fixture.json`): with the fixture's code claims as
  "tasks", unanimous verdicts of three fake pack-readers give 0 wrong; a dead third voter gives no decision and the
  single reader answers (counter `votes_overridden` stays 0).
* The existing Gate 1 tests stay green **unchanged** with `claim_vote = off`; with it on and no voters configured the
  result is byte for byte the single reader's.
* Numbers the round reports: `votes_asked`, `votes_decided`, `votes_overridden`, requests per candidate (3), 0 crashes.

**Layer 2 — the operator, live (a step in `docs/claim-check/RUNBOOK.md`, not part of the round).**
1. Run `main.py --dry-run` / `--auto` planning on one real target and keep the Architect's candidates (the
   `--validate-plan` path writes them without executing).
2. Settle the answer for each candidate by hand: true bug / false positive. `scripts/run_claim_check.sh --truth`
   (CC-8) writes `truth.csv` and a `pending.md` for the ones the vote cannot settle; the operator fills those.
   The set is then recorded in the repository with its target sha (a known-answer set, like the CC-0 fixture).
3. Run Gate 1 twice on the set, `claim_vote = off` and `on`: false positives let through, true bugs rejected,
   seconds per candidate, requests per candidate. Numbers go to the round's `SUMMARY.md`.
4. Pass: with the vote on, false positives let through are not more, true bugs rejected are not more, and the
   vote decided at least a third of the candidates; otherwise the flag stays `off`.

## Questions, answered

1. **Which voters, and the rate limit.** By measured wrong rate over the 150 fixture claims (70 base + 80 head), two live
   runs (table below). Default `vote_profiles`: **step-5-preview, muse-spark-1-3-contributor, nemotron-3-ultra** (three families;
   step-5 replaces mimo-v2-6-flash, which flips its verdict between runs; the 129 of 150 settled, 0 wrong were measured with mimo). Spare: agnes-3-0-flash, hy3, mimo-v2-6-flash (never alone). Excluded: laguna-s-2-1 (8 wrong of
   136) and deepseek-v4-flash (4 + 1 wrong); sensenova-6.8-flash-lite is allowed but not a default (6 wrong of 129 alone, and its
   rate limit, below). The vote is a shortcut for **every** candidate that reaches Stage B, but it is capped: `[gate1]
   vote_max_per_run` (default 200 candidates) and a per-provider limit through the `claim_vote` throttle, so a run of hundreds of
   candidates is three requests each at the voters' pace, never a flood; past the cap the single reader answers as today.
2. **Is a unanimous `FALSE` enough to reject.** Not alone in the first version: reject on unanimous `FALSE` of three families
   **and** the single reader not saying `confirmed` (`vote_veto_by_reader = on`). A unanimous `TRUE` confirms. Relax the veto only
   with the measured set of layer 2.
3. **The pack for a task.** The cited block (CC-5 pack of the symbol), plus its git header chunk; no callers in the first
   version. A bigger pack is a later measured change.
4. **Round-trip time.** Voters run in parallel; each has a timeout (`vote_timeout_sec`, default 60); a timeout or error is a
   missing vote, so the candidate falls back to the single reader and nothing blocks.
5. **Only one model.** `vote_quorum = 1` is never the default and not in this ticket's acceptance. If an operator sets it, the
   output says `quorum=1` and the model, and it accepts a verdict only with a code-verified quote and the same verdict in every
   one of N runs. The roster for it is by measured wrong rate: hy3 0 of 128, muse-spark 0 of 130, north-mini-code 0 of 93 qualify;
   laguna 8 of 136, sensenova-6.8 6 of 129, agnes-2-5 5 of 128 do not.

## Live numbers (round 280, free models, real runs; the voting of CC-6 on the CC-0 bench)

Six voter sets of three, fixture of 80 claims and 30 real claims, one run each. Settled = every voter
committed and all agreed, quotes checked by code.

| voters | fixture settled / wrong | real settled / wrong |
|---|---|---|
| hy3, deepseek-v4-flash, nemotron-3-ultra (kenary, tokenharbor, bynara) | 57 / 0 | 11 / 0 |
| cohere north-mini-code, dots-3-note, laguna (openrouter2 + kenary) | 50 / 0 | 11 / 0 |
| nemotron-3-super, hy3, agnes-2-5 (kenary) | 56 / 0 | 10 / 0 |
| agnes-3-0-flash, nemotron-3-ultra, laguna (kenary) | 63 / 0 | 16 / 0 |

Per voter alone (settled by itself, quote checked): hy3 76 right of 76 and 18 of 18; agnes-3-0-flash 79 of 79
and 20 of 21; nemotron-3-ultra 68 of 68 and 21 of 21; dots-3-note 78 of 79 and 19 of 19; **laguna 72 of 77 and
23 of 25; deepseek-v4-flash 65 of 69 and 12 of 13**. Zero wrong across about 330 settled verdicts, while the
weakest single voters were wrong on 5 to 7 % of theirs: **the unanimity rule, not any one model, is what
makes the vote safe.** Cost: a code claim is 0.5 request per voter (two to a request); a Gate 1 candidate is
one claim, so `code_batch = 1` there and a candidate costs three requests.

### Best voter sets by size (rounds 280 + 285 + the sensenova run)

Eleven voters, 150 claims (fixture base 70 + head 80, truth from `truth_base`/`truth_head`), one run each, settled = every
voter committed with a quote the code verified and all agree. Searched over all sets of that size.

| size | best set | settled / wrong | best set with sensenova-6.8 | settled / wrong |
|---|---|---|---|---|
| 3 | mimo-v2-6-flash, muse-spark-1-3, nemotron-3-ultra | 129 / 0 | mimo, muse-spark, sensenova-6.8 | 118 / 0 |
| 5 | + agnes-3-0-flash, step-5-preview | 125 / 0 | agnes-3, mimo, muse-spark, nemotron-ultra, sensenova | 116 / 0 |
| 7 | agnes-3, hy3, mimo, muse-spark, nemotron-super, nemotron-ultra, step-5 | 115 / 0 | agnes-3, laguna, mimo, muse-spark, nemotron-ultra, sensenova, step-5 | 111 / 0 |
| 9 | agnes-2-5, agnes-3, laguna, mimo, muse-spark, nemotron-super, nemotron-ultra, sensenova, step-5 | 103 / 0 | (same) | 103 / 0 |

More voters settle fewer (129 → 125 → 115 → 103) and are no safer: the best three are already at 0 wrong. **Three is the
minimum that the quorum allows and the best value; five buys nothing measured; seven and nine only cost requests.** Per voter
alone, wrong of decided: hy3 0/128, muse-spark 0/130, north-mini-code 0/93, agnes-3 2/135, mimo 2/134, nemotron-super 2/135,
nemotron-ultra 2/138, step-5 2/135, agnes-2-5 5/128, sensenova-6.8 6/129, laguna 8/136.

**Sensenova in the vote (token.sensenova.ai, free).** The run asked 219 requests (sensenova-6.8: 73, 6.7: 73, nemotron-ultra the
rest) and met **79 HTTP 429 "rpm exhausted"** (4 of them ran out all 5 retries at 15 s). 6.8 still decided 129 of 150 claims; **6.7
decided none** (every request ended in 429 or error) and counts as a dead voter. So sensenova works as one voter of three, slowly,
and not as three: a Gate 1 run of hundreds of candidates would sit in its rate limit. It is the reason for the cap in answer 1.

What the runs found that fake voters did not (all fixed in CC-6): a quote copied over two code lines carries
a raw newline; a reply of separate JSON objects without a list; a dead voter counted in the unanimity.
Gate 1 inherits the fixes, and its own test must use the **real reply shapes**, not only a clean JSON.

### What the numbers say about the open questions

* Q2 (is a unanimous `FALSE` enough to reject): it was right every time it occurred in these runs, and no
  single voter is trustworthy alone. Draft rule: reject on unanimous `FALSE` of three families **and** keep
  the existing single reader's `confirmed` as a veto in the first version (`vote_veto_by_reader = on`), to
  be relaxed only with a measured Gate 1 set.
* Q5 (one model): see the single-voter text above; the table now says which models qualify (wrong on at most
  1 in 70 in these runs: hy3, agnes-3-0-flash, nemotron-3-ultra, dots-3-note) and which never do (laguna,
  deepseek-v4-flash).

## Not in scope

Changing Stage A, A0 or C; the Coder, executor or validators; the unanimity rule; Lenz.
