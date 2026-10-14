# CC-14 — the round's claims at the judge: did each entry do what its ticket says (`judge_epic_round.py --claims`)

**Status:** queued
Cut it into `epic-tasks/` with the first word `open` when the round starts.
**Severity:** MEDIUM (it is the one question of a round the judge still answers by reading, and the instrument for it exists)
**File:** `scripts/judge_epic_round.py`, `scripts/claim_diff.py`, `tools/contest/export.py`, `tools/contest/draft.py`
**Symbol:** `main` of `judge_epic_round.py` (a third phase, `--claims`), `judge_both` (a base side judged once and reused), `write_summary` (the judge line), `SECTIONS` / the draft check (an optional `## Claims`)
**Round:** —
**Size:** M
**Depends on:** CC-7 (285, `claim_diff.py`, landed), CC-11 (293, `--accept committed`, landed)
**Also touches:** `docs/kilo-contest/RUN-THE-KILO-CONTEST.md` (hand-over, judging), `docs/claim-check/RUNBOOK.md` ("Closing a round"), `docs/collect-epics/RUN-THE-EPIC-COMPETITION.md` (the scorecard), `tests/test_judge_epic_round_claims.py`

---

## Why

A finished contest round is judged in two machine phases and one human one. `judge_epic_round.py` prints the score table (hard gates, commits, files,
tests) and, with `--cross`, every entry's own tests run on every implementation. Whether an entry **did what its ticket says** is left to the judge's
reading of the diff — the question every round turns on, and the one the machine side never touches.

CC-7 built the instrument for exactly that question: the same claims judged at the branch point and at the candidate, with the code of each side in
front of the voters, and a change per claim (`FIXED` / `STILL` / `NEW` / `GONE` / `UNCLEAR`). `docs/claim-check/RUNBOOK.md` ("Closing a round") tells
the operator to run it by hand, once per candidate. No round has: it is one command per entry, with refs to find, voters to name and a claims file to
write, and nothing in the round's `SUMMARY.md` points at it. The contest code and its runbook do not mention it at all.

What it is worth, measured (RUNBOOK, five live runs of three free voters on the fixture: 20 `fixed`, 10 `still`, 2 `gone`, 2 `new` claims): `FIXED` 17–18
of 20, `STILL` 8–10 of 10, `GONE` 2 of 2, `NEW` 1–2 of 2, and **a claim that was still true reported `FIXED`: 0 in every run**. The rest is `UNCLEAR` — an
honest answer, not a failure. So it is a strong lead and never a verdict: a column next to the cross phase, not a hard gate.

Cost is why it has to be built and not just scripted: three voters, a code claim at `code_batch = 2` is 0.5 request per voter per side. Ten claims and
three entries run as three separate `claim_diff` calls judge the **same base three times** — every entry starts from the same tree, so the base side of
every claim is the same question asked again.

## What it does

* **`## Claims` in a ticket** (optional; after `## Acceptance`). One bullet per claim, the expected change in brackets:

  ```
  ## Claims
  - [new] `Gate1Vote.collect_read` returns None when `[gate1] vote_collect` is off.
  - [fixed] `check_collect` accepts an artifact built by another collector version.
  - [still] `load_readonly` never writes to the collect directory.
  ```

  The words are `claim_diff`'s four (`new`, `fixed`, `still`, `gone`); a `fixed` claim states the **defect** (true at base, false after the fix), a `new`
  claim the behaviour the ticket adds. Code spans name the code, so the claim anchors (CC-1) and is judged on its evidence, never as `world`. The draft
  stage (`tools.contest draft`) accepts the section and its format spec documents it; it does not require one.

* **`judge_epic_round.py --claims [FILE]`** — a third phase, after the score table and after `--cross` when both are given. The claims come from FILE
  (anything `claim_diff.py` reads) or, with no FILE, from the round's ticket's `## Claims`. For every scored worktree whose hard gates passed (an entry
  that failed `commits` or `pushed` is not worth the requests and is listed as skipped): `claim_diff`'s own `judge_both` with the target the worktree,
  base `--base`, head the worktree's `HEAD`. The voters are `[claim_vote] llm_profiles` (or `--claims-profiles`), the rule `[claim_vote] accept`, one run
  (`--claims-runs N` for more), packs with the collect facts as `claim_diff` builds them.
* **The base side is judged once per round.** `judge_both` takes an optional recorded base side (the `base` part of an earlier result) and reuses it when
  the claims, the base sha, the voters, the runs and the rule are the same; any difference is a refusal, not a silent re-use. The first entry pays for the
  base, the others for their head only. `claim_diff.py --base-votes FILE` is the same reuse from the command line (`votes_base.json` of an earlier run).
* **One line before the first request** with the cost: claims, voters, runs, entries, and the requests it adds up to (`requests_of`, base once plus a head
  per entry), so a round of eight entries is not started blind. A missing voter section is a refusal naming it (exit 2) before any request.
* **The table**, one row per entry: `FIXED STILL NEW GONE UNCLEAR n/a | expect met / with an expect`, then every claim whose change is not its `expect`,
  with the entry, both sides' verdicts and the evidence ids (`claim_diff --check`'s own lines). Like the cross phase's `behaviour` class it is **a lead**:
  read the evidence, reproduce by hand. With `--cross-out DIR` the table is also written to `DIR/claims.md` and each entry's `delta.json`,
  `votes_base.json` (once) and `votes_head.json` go to `DIR/claims/<entry>/`.
* **Exit codes do not change** for a mismatch (a model's reading never fails a round); 2 for a refusal of the claims (an unknown word, an `expect` on a
  `world` claim — `judge_both` already refuses that one — or an empty file).
* **The hand-over.** `write_summary` adds `--claims` to the printed judge line when the round's ticket has a `## Claims` section, so the command the round
  ends with is the one that runs it.

## Tests (sketch)

`tests/test_judge_epic_round_claims.py`, no network: a small git repository, a base commit and two entry worktrees (one makes the change the claims
describe, one leaves the cited code alone), and the `vote` seam of `judge_both` filled by scripted voters that answer from the side's own tree (TRUE when
the cited line is there, with a real quote of it), in the reply shapes the live voters give (a JSON array of objects, a quote over two lines).

* The table counts per entry: the entry that made the change meets its `new` and `fixed`, the other gets `STILL` / not `NEW` for them and both appear
  under the mismatches with their evidence ids.
* The base side is asked once: the number of base requests is the same for one entry and for three; a recorded base of other voters, another sha or
  another rule is refused with the field that differs.
* `## Claims` parsing: the four words; an unknown word is exit 2 naming the line; no section and no FILE is no phase and one line saying so; an `expect`
  on a world claim is the refusal `judge_both` gives.
* An entry that failed a hard gate is listed as skipped and costs no request.
* `write_summary`: the judge line has `--claims` exactly when the ticket has the section.
* The cost line is printed before the first request, and its number is the requests the scripted voters then received.

## Acceptance (two layers)

**Layer 1 — offline, in the round.** The tests above; the existing suites green unchanged (`.smoke_tests/ .regression_tests/`, `tests_bugfix -n 4`); a
black box (`contest-bench/296/blackbox_296.py`: a fake model server, `judge_epic_round.py` run as a process on two prepared worktrees with a ticket that
has a `## Claims` section) whose table, `claims.md` and request count match the scripted answers, and whose base requests do not grow with a third entry.

**Layer 2 — the operator, live, one command.** On a finished round whose worktrees are kept (the next CC round, or 295 if its worktrees still exist), with
six to ten claims written for its ticket: `python3 scripts/judge_epic_round.py --round NN --base <sha> --runs <out>/wt --cross --claims`. Pass: the
landed entry meets every `new` and `fixed` that is not `UNCLEAR`, no entry that left the cited code untouched gets a `FIXED` or `NEW` for it, and the
requests are the cost line's. The record goes into `docs/kilo-contest/RUN-THE-KILO-CONTEST.md` ("Judging a finished round") with the table verbatim.

## Contest

A mini contest, **Opus, Sonnet and Haiku by hand**, like CC-12 and CC-13: offline acceptance, a deterministic black box; the risk is the base re-use (a
stale base passed off as this round's) and the parsing of a ticket section, not the voting, which is `claim_diff`'s and unchanged.

## Questions

1. **Why not a hard gate.** Free voters leave a share `UNCLEAR` and are wrong now and then on `NEW`; a round must not be lost to a model's reading.
   The measured zero is on the costly direction only (a still-true claim reported `FIXED`). If a few rounds show the column never misleads, a later
   ticket can make `fixed`-not-met a gate.
2. **Who writes the claims.** The ticket's author, as part of the ticket (the drafter can be asked to propose them; it is not in scope). A ticket without
   the section is judged as today.
3. **Why reuse the base and not run `claim_diff` per entry.** Every entry starts from the same base: its judgement is the same question asked again, and
   with eight entries it is most of the requests. The refusal on any difference keeps the re-use honest.

## Not in scope

Changing the voting, the acceptance rule, `classify_delta` or the pack; Lenz (`claim_extract.py`) as the source of claims; making the claims a gate;
`--auto`'s Gate 1 (CC-10, CC-12, CC-13); the contest runner itself (`tools.contest run` does not run the judge).
