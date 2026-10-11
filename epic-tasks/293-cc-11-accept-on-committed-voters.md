# CC-11 — accept a verdict on three committed voters, not on all of them

**Status:** FINAL — ready to be cut into `epic-tasks/`. Every number below is from live runs of the
runbook on free models (nine runs, 2026-10-11) and from `contest-bench/cc/replay_rules.py`, which
reproduces them offline from `contest-bench/cc/free_votes.json`.
**Severity:** MEDIUM (it decides how many voters are worth running; today more than three only cost requests)
**File:** `scripts/claim_vote.py`
**Symbol:** `tally` (the `unanimous` flag), `main` (`--accept`), `[claim_vote] accept`
**Round:** —
**Size:** S
**Depends on:** CC-6 (landed: `tally`, `votes.json`), CC-7 (`classify_delta` reads `unanimous`), CC-8 (`settled` needs three families)
**Also touches:** `scripts/claim_diff.py`, `scripts/claimcheck_truth.py`, `tools/claimcheck/truth.py` (`how`, `checked_by`), `docs/claim-check/RUNBOOK.md`, `tests/test_claim_vote_accept.py`

---

## Why

The acceptance rule of the epic is *unanimity of the voters*, and `tally` implements it as
`unanimous = (one verdict among the committed) and committed >= quorum and committed == len(voters)`.
The last clause means **every voter that produced any vote must also commit on this claim**. A voter
that answered `UNSURE`, lost a batch, had its quote rejected by the code, or died halfway blocks the
claim for everybody. So under today's rule **adding voters makes the check settle fewer claims**:

| voters (best sets, 150 fixture claims) | settled / wrong |
|---|---|
| 3 | 138 / 0 |
| 5 | 132 / 0 |
| 7 | 108 / 0 |
| 9 | 91 / 0 |

and nothing is gained in safety: the three are already at 0 wrong. That contradicts the one practical
reason to run more than three, which is that **free models die, rate-limit and go quiet, so five
voters are meant to leave at least three that work**. With the rule below, replayed on the same
recorded verdicts:

| set | rule `all` (today) | rule `committed` |
|---|---|---|
| default trio (mimo, muse-spark, nemotron-ultra) | 136 / 0 | 136 / 0 |
| 3 (nemotron-ultra, muse-spark, agnes-3) | 138 / 0 | 138 / 0 |
| 5 (+ step-5, hy3) | 132 / 0 | **146 / 0** |
| 7 (+ nemotron-super, vercel `ling-3.1-flash`) | 108 / 0 | **146 / 0** |
| 9 (+ agnes-2-5, mimo) | 91 / 0 | **146 / 0** |
| 5 with two weak voters (laguna: 7 wrong alone, north-mini-code: 2) | 86 / 0 | 139 / 0 |

148 of the 150 claims have a truth; the rule accepts 146 of them, none wrongly. Over **every** subset
of the 15 recorded voters (weak ones included): size 7 and 9, 0 of 6435 / 5005 subsets accepted a wrong
claim; size 5, 12 of 3003 did (at most 3 claims, always with a weak voter in the set); size 3, 20 of
455 (the same rule as today for three). So the rule is safe where the roster is the measured one and
**seven voters are safe even with weak ones mixed in**; five are safe only from the roster
(`docs/claim-check/RUNBOOK.md`, "Voters").

## What it does

A second acceptance rule, chosen by `[claim_vote] accept = all | committed` (default `all`: every
existing run, test and file stays byte for byte) and `--accept all|committed` on `claim_vote.py` and
`claim_diff.py`.

```
rule all        accepted  <=>  one verdict among the committed
                              and committed >= quorum
                              and committed == len(voters)          # today
rule committed  accepted  <=>  one verdict among the committed
                              and committed >= quorum               # abstainers do not block
```

* **Committed** is unchanged: a model whose own majority over its runs is `TRUE` or `FALSE`, with a
  quote the code verified; `UNSURE`, a missing vote, a rejected quote are *not* committed.
  **Quorum** is unchanged: `MIN_COMMITTED = 3`, and `quorum=1` still prints its warning.
* A dissent among the committed (TRUE, TRUE, FALSE) is **not accepted** under either rule: it is `SPLIT`
  or a plurality, never a settled row. The rule only stops *abstention* from counting as dissent.
* `tally` keeps the field name `unanimous` (CC-7's `classify_delta`, CC-8's `settled`, CC-10's Gate 1
  and the saved files read it). Under `committed` it means "unanimous among the committed". Each
  row gains `"abstained": [model, …]` (the voters of the run that did not commit on this claim,
  `[]` under `all` when it is accepted) and `votes.json` gains the top-level `"accept"`. An old
  `votes.json` has neither and is read as `all`.
* **Families stay required.** `settled()` (CC-8) still asks for `MIN_FAMILIES = 3` families *behind
  the verdict*: three committed voters of one family are one opinion. Under `committed` the families
  are counted over the committed voters only, so `quorum` and the families rule are both met by three
  independent models and neither is weakened.
* `truth.csv`: `checked_by` names the families that committed, `how` says `3 of 5 voters committed`
  (the number of those who did of those who ran), so a row accepted over two abstainers is visible
  as such. `pending.md` lists, for a claim not accepted, which voters abstained.
* A voter with **no vote at all in any run** is still dropped from `voters` first (round 280's
  rule): under `all` it no longer blocks, as before; under `committed` it is irrelevant.
* `claim_diff.py --accept committed` hands the rule to both sides' `tally` calls; the same rule must
  be on both sides, and the saved `delta.json` records it.
* `run_claim_check.sh` passes `--accept` to `claim_vote.py` as it passes every unknown option.
* **The default does not change in this ticket.** `accept = all` stays until the operator step of CC-10
  (a known-answer set at Gate 1) shows the same or fewer wrong with `committed`. The runbook says
  which to use meanwhile: three voters need nothing; five or more should run `--accept committed`.

## Why not simpler

* *Lower the quorum instead.* `quorum` is how many must commit; the problem is that the uncommitted
  *block*. `quorum = 3` of five with `all` would still require all five.
* *Majority of the voters (3 of 5 agree, 2 dissent) is accepted.* No: a dissent is information, and
  the measured zero is for a rule that needs agreement among everybody who committed.
* *Drop the voters that abstain before voting.* A voter abstains per claim, not per run; what is
  dead for the whole run is dropped already.

## Tests

`tests/test_claim_vote_accept.py`, no network, fake voters that answer in the real reply shapes
(a quote over two lines, separate JSON objects, a dead voter, a stray quote in the prose).

| Test | What it pins |
|---|---|
| `test_default_is_all_and_unchanged` | every existing `tally` test stays green; with no option the rows have no `abstained` and `votes.json` has no `accept` key difference beyond `"accept": "all"` |
| `test_abstainer_does_not_block_under_committed` | 5 voters, 3 say TRUE, 2 say UNSURE: `unanimous` under `committed`, not under `all`; `abstained` names the two |
| `test_dissent_is_not_accepted_either_way` | TRUE, TRUE, TRUE, FALSE, UNSURE: not unanimous under both rules |
| `test_two_committed_are_below_quorum` | 5 voters, 2 commit: UNSURE under `committed` |
| `test_dead_voters_cost_nothing` | 5 voters of which 2 return nothing: the table equals the three-voter table for the same claims |
| `test_replay_matches_the_oracle` | the verdicts of `free_votes.json` fed through `tally` for the sets 3, 5, 7, 9, default3, 5weak give exactly `replay_rules.py`'s counts under both rules (the table above) |
| `test_families_still_required` | three committed voters of one family under `committed` give no `REAL`/`FALSE` truth row (CC-8's `settled`) and go to pending as "fewer than 3 model families" |
| `test_truth_row_says_how_many_committed` | the `how` column and `checked_by` of a row accepted over two abstainers |
| `test_accept_option_and_ini` | `--accept` beats `[claim_vote] accept`; an unknown value is exit 2 with one line; `claim_diff.py --accept` reaches both sides and `delta.json` records it |
| `test_old_votes_json_is_read_as_all` | a saved file without `accept` loads and rebuilds as before |

## Acceptance (two layers)

**Layer 1 — offline, in the round (no network, no key).**
* `python3 contest-bench/cc/replay_rules.py` is the oracle: its table is the one above. The
  candidate's tests reproduce it through `tally` (test `test_replay_matches_the_oracle`).
* The existing suites stay green unchanged with the default: `python3 -m pytest .smoke_tests/
  .regression_tests/`, then `python3 -m pytest tests_bugfix -n 4 -q` (two roots, run one after the other).
* Black box (`contest-bench/293/blackbox_293.py`, written by the judge before the round, as for 285 and
  290): the candidate's `scripts/claim_vote.py` and `scripts/claim_diff.py` as processes against a local
  fake model server with five voters, two of which answer `UNSURE` on a fixed list of claims or return
  HTTP 404; with `--accept committed` the table has the claims accepted that `--accept all` leaves
  open, none wrong, `delta.json` records the rule, and CC-7's FIXED/STILL numbers are not lower than
  with `all`.

**Layer 2 — the operator, live (a step in `docs/claim-check/RUNBOOK.md`, not part of the round).**
Run `claim_diff.py … --accept committed` with the five-voter roster and `--runs 3` on the fixture
(about two hours at kenary's pace): wrong settled 0, `still_as_fixed` 0, accepted not fewer than the
three-voter run. The default flips to `committed` only with CC-10's Gate 1 set showing no more wrong.

## Edge cases to handle

* All voters commit (the common case): the two rules give the same row, `abstained` is `[]`.
* Exactly `quorum` committed and the rest silent: accepted under `committed` — and rejected by
  CC-8's family rule if they are fewer than three families.
* A voter that voted in run 0 and not in run 1: its own majority decides, as today.
* `blind` claims (a code claim without `--target`): still `CODE-CHECK`, never accepted by either rule.
* `votes.json` from `claim_diff.py` has two sides with one rule; a mismatch is refused.

## Questions, answered

1. **Competition of free models, or the three cloud models?** The three cloud models (Opus 5, Sonnet 5,
   Haiku 5), a normal round like CC-8 and CC-9. The change is about forty lines in one function plus
   tests, its acceptance is deterministic and offline (the oracle and the black box), and nothing in
   it depends on how well a free model codes. A free-model contest would only add noise to a ticket
   whose whole risk is the rule, and the rule is measured above.
2. **Does it embed the check into our pipeline (`--auto`)?** No. That is CC-10 (292), Gate 1, behind
   `[gate1] claim_vote = off`. CC-11 is the voting rule CC-10 will run: land CC-11 first, then CC-10
   with five voters and `accept = committed`, and its operator step decides the defaults.
3. **Is five enough, or seven?** Five from the measured roster; seven when the roster has weak
   members. More only adds requests (146 of 150 at five, seven and nine alike).

## Not in scope

The roster (that is the runbook's table), the quorum, the families rule, the quote check, Lenz, changing
`classify_delta`, flipping the default.
