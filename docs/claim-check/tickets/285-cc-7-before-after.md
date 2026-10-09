# CC-7 — before and after: the same claims judged at two commits

**Status:** draft
**Severity:** MEDIUM (turns "the fix landed" from a reading into a measurement)
**File:** `tools/claimcheck/compare.py`
**Symbol:** `compare`, `Delta`, `classify_delta`
**Round:** —
**Size:** M
**Depends on:** CC-6 (voting with packs), CC-2 (`Target`)
**Also touches:** `scripts/claim_diff.py`, `docs/claim-check/BEFORE-AFTER.md`, `tests/test_claimcheck_compare.py`

Runs in parallel with CC-8: CC-7 owns `compare.py`, `claim_diff.py` and its own page
`BEFORE-AFTER.md`; CC-8 owns `truth.py`, `claimcheck_truth.py` and the edits to
`run_claim_check.sh` and `RUNBOOK.md`. They share no file.

---

## Why

A review says "`_commits_above()` returns 0 when `rev-list` fails" and a fix lands. Did it
fix it? Today that is a reading of the diff plus a test someone remembers to run, and the
memory of past rounds names the trap: a ticket marked done that is not fixed. The same
claim, judged with the evidence at the commit before and at the commit after, answers it:
`TRUE` before and `FALSE` after is a fix; `TRUE` in both is not. The diff itself is part of
the evidence at the second commit (CC-4's `diff:` chunks), so the voter can see what changed.

It is also how a cloud branch of patches is checked against what has already landed: take
the claims the branch's tickets make, run base = `main`, head = the branch.

## What it does

### `claim_diff.py claims.json --target REPO --base REF --head REF [--fetch] [--profiles …] [--out DIR] [--check]`

1. Opens two targets (`Target.open` for `base`, for `head`; two worktrees, two pinned shas).
2. Judges every claim **twice** through the CC-6 machinery: at `base` with the pack built
   at `base`; at `head` with the pack built at `head` **and** the `base..head` diff chunks
   for the anchored paths. The voters, runs, pacing and retry budget are the same as
   `claim_vote.py`'s; the two judgements of one claim use the same voters.
3. Calls `compare(base_votes, head_votes, base_resolved, head_resolved)` and writes
   `delta.json` plus a human table.

### `compare(...) -> list[Delta]`, `classify_delta(...)`

`Delta(claim, base, head, change, evidence_base, evidence_head)`; `change` is one of:

| change | Rule |
|---|---|
| `FIXED` | unanimous `TRUE` at base **and** unanimous `FALSE` at head |
| `STILL` | unanimous `TRUE` at both |
| `NEW` | not unanimous-`TRUE` at base (it was `FALSE`, or unknown) and unanimous `TRUE` at head |
| `GONE` | the claim's primary anchor (symbol or path) resolved at base and does not at head — the code the claim was about no longer exists under that name; the claim is not decided at head |
| `UNCLEAR` | anything else: a non-unanimous side, `UNSURE` on either, `FALSE` at both |

`FALSE` at both is `UNCLEAR` on purpose: the claim was never true here; "fixed" would be a lie.
A `world` claim has no before and after; it is judged once and reported as `n/a`.

### Input and `--check`

Claims may carry `"expect": "fixed" | "still" | "new" | "gone"` (the fixture's claims do).
With `--check` the command exits `1` when any claim with an `expect` has another
`change`, and prints each mismatch with its evidence ids; claims without `expect` never
fail it. This makes the command a gate for "this branch fixes what its tickets say".

### `BEFORE-AFTER.md`

A short page: what the four words mean, the exact rule above, one worked example
(`FIXED`), the `--check` use, and the honest limit: a `FIXED` is "the code at head no longer
shows what the claim says", not "the behaviour is fixed" — runtime behaviour the text does
not show stays `UNCLEAR`.

## Tests (`tests/test_claimcheck_compare.py`)

| Test | What it pins |
|---|---|
| `test_classify_delta_table` | 25 rows of (base verdicts, head verdicts, anchors found) → the change, including every cell of the matrix |
| `test_false_at_both_is_unclear` | |
| `test_world_claim_is_not_applicable` | |
| `test_gone_when_the_symbol_vanishes` | the function renamed at head → `GONE` even when the votes would say `FALSE` |
| `test_end_to_end_on_a_temporary_repo` | a repo with base and head commits (the head removes `check=False`); fake voters that answer from the pack text (`TRUE` iff it contains `check=False`) → `FIXED`; an untouched claim → `STILL` |
| `test_head_pack_contains_the_diff_chunks` | the prompt at head carries a `diff:<base7>..<head7>` chunk for the anchored path; the base prompt does not |
| `test_same_voters_judge_both_sides` | the call log |
| `test_check_exit_codes` | all match → 0; one mismatch → 1 and the line names the claim and the ids |
| `test_claims_without_expect_never_fail_check` | |
| `test_two_worktrees_and_cleanup` | two opens, both closed; the operator's checkout untouched |
| `test_delta_json_shape` | keys, and the shas of both sides |
| `test_base_equals_head_is_all_still_or_unclear` | no `FIXED` possible |
| `test_no_network` | `socket` blocked |

## Acceptance (the operator's bench, `contest-bench/285/acceptance_285.py`)

On the fixture with the fake pack-reader voter: the 20 `fixed` claims → `FIXED` (≥ 19), the 10 `still` → `STILL` (≥ 9), the 2 `gone` → `GONE` (2), the 2 `new` → `NEW` (2), no claim
`FIXED` that the key says `still`. Then the live table of `run_cc.sh --diff` over the same
file, with the three real voters, against the target of `EPIC-CC.md` §6.2 (≥ 90 % right on
the fix claims); a miss is a fix ticket.

## Edge cases to handle

A head where the file moved (rename detection helps CC-4's chunk; `GONE` is for names that
vanish, a moved file with the same symbol resolves at head by CC-1's module-qualified
rule); a head that is an ancestor of base (a reverted fix: `NEW`); a claim about a commit
that exists only at head (dangling at base: `NEW`/`UNCLEAR` by the rule); `base` or `head`
the same sha twice.

## Not in scope

Running tests at either commit, judging a diff that has no claims, writing `truth.csv` (CC-8).
