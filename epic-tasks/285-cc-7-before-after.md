# CC-7 — before and after: the same claims judged at two commits

**Status:** open
**Severity:** MEDIUM (turns "the fix landed" from a reading into a measurement)
**File:** `tools/claimcheck/compare.py`
**Symbol:** `compare`, `Delta`, `classify_delta`
**Round:** 285
**Size:** M
**Depends on:** CC-6 (landed: voting with packs and quotes), CC-2 (`Target`), CC-5 (`build_pack` with `base`/`head`)
**Also touches:** `scripts/claim_diff.py`, `scripts/claim_vote.py` (only to share what `main_target` already does; its behaviour and tests stay as they are), `docs/claim-check/BEFORE-AFTER.md`, `docs/claim-check/RUNBOOK.md` (one line), `tests/test_claimcheck_compare.py`, `tests/test_claim_check.py` (only if `claim_vote.py` gains a shared helper)

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
2. Judges every claim **twice** through the CC-6 machinery (`prepare_target`, `ask_v2`, `tally`
   of `scripts/claim_vote.py`; see "What CC-6 actually returns" below, which wins where this
   text differs): at `base` with the pack built at `base`; at `head` with the pack built at
   `head` **and** the `base..head` diff chunks for the anchored paths (`prepare_target(...,
   base=<base sha>)` already does that). The voters, runs, pacing and retry budget are
   `claim_vote.py`'s own; the two judgements of one claim use the same voters, and the jobs
   of both sides go through **one** pool, so the per-host pacing is shared.
3. Calls `compare(...)` and writes `delta.json` plus a human table. `delta.json` is
   `{"base": "<sha>", "head": "<sha>", "deltas": [{"id": …, "claim": …, "change": "FIXED", "base":
   {"verdict": …, "unanimous": …, "evidence": […]}, "head": {…}}, …]}`: `id` is the claim's
   `id` when it has one (the fixture's do), and **`contest-bench/cc/score_cc.py delta.json
   claims_fixture.json` reads it unchanged** (it matches a delta to its claim by `id`, else by
   text, and reads `change`).

### `compare(...) -> list[Delta]`, `classify_delta(...)`

`Delta(claim, base, head, change, evidence_base, evidence_head)`; `change` is one of:

| change | Rule |
|---|---|
| `FIXED` | unanimous `TRUE` at base **and** unanimous `FALSE` at head |
| `STILL` | unanimous `TRUE` at both |
| `NEW` | `FALSE` at base (a plurality of `FALSE` is enough; unsure, split and `TRUE` with a dissenter are not) and unanimous `TRUE` at head — changed after round 285's live run, where "not unanimous `TRUE`" labelled two claims that were true all along `NEW` |
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

## What CC-6 actually returns (landed; read before you build)

The text above was written before the voting existed. Where it differs, this section wins.
Everything here was read from the landed code and run live (round 280, runs A and B, on
real free models).

* **`Target.open(repo, ref, *, scratch, fetch=False, expect_sha=None)`** is a context manager;
  `target.sha` is the full commit, `target.view()` the `RepoView`, `target.warnings` a list.
  **Two targets of one repository can be open at once with the same `scratch`** (probed:
  base and head of the fixture repo, both views readable). Open both in one `with`, never
  the operator's checkout.
* **`prepare_target(claims, target, budget, base=None) -> (items, meta)`** in
  `scripts/claim_vote.py`. `items[i]` is `(claim_text, pack_or_None)`; `meta[i]` is
  `{"kind", "dangling", "sha"}`. `kind` is CC-1's (`code`, `mixed`, `world`; a claim about a
  commit or ticket is `code`); a `world` claim has no pack. With `base` given the pack also
  holds the `base..target.sha` chunks. The head side calls it with `base=<base target's sha>`;
  the base side calls it with `base=None`.
* **What the head pack holds (probed on the fixture).** For `config.load_timeout` at head:
  `src:config.py:18-20`, `diff:d36d59e..5b24122:config.py:17-23`,
  `diff:d36d59e..5b24122:config.py:33-45`. The diff chunks are the **whole file's** hunks,
  so a claim about `parse_bool` is also shown `load_timeout`'s hunk: that is by CC-4's design,
  not a bug. **The `src:` chunk id has no sha in it:** `src:config.py:18-20` is the id at base
  *and* at head, with different text and, after a change, often a different span. Never treat
  an equal chunk id on the two sides as "the same evidence"; `Delta.evidence_base` and
  `evidence_head` are lists of ids read **per side**.
* **`ask_v2(ref, items, run, seed, parser, timeout, batch, code_batch, fixed) -> dict`**: one
  voter, one run. The row: `{"model", "run", "votes": {item index: "TRUE"|"FALSE"|"UNSURE"},
  "accepted": {index: {"chunk", "quote"}}, "rejected": [{"claim", "verdict", "chunk", "quote",
  "reason"}], "error"?, "raw_head"?}`. `votes` values are already quote-checked strings (a
  vote the evidence did not support is `UNSURE`), `rejected[*].reason` is
  `chunk_not_in_pack` or `quote_not_in_pack`. A model that cannot be reached is a row with
  `votes == {}` and an `error`, never an exception. The ini lookup, the HTTP call, the pacer
  (`PACER`) and the retry budget are inside it: the compare code calls it, it does not rebuild it.
* **`tally(claims, results, meta=meta) -> list[dict]`** (the second argument is the list of
  `ask_v2` rows, all voters and runs of one side). A row per claim: `id`? (when the claim has
  one), `claim`, `truth`, **`verdict`** (the cross-model verdict among the committed votes:
  `TRUE`/`FALSE`/`SPLIT`/`UNSURE`; `quorum` 3), **`unanimous`** (bool), `by_model`, `all_votes`,
  `kind`, `dangling`, `needs_code`, `sha`, `evidence` (the chunk ids the accepted votes cited),
  `quotes`, `rejected` (counts by reason), `downgrades`. **The rule for this ticket: a side's
  vote is "unanimous `TRUE`" exactly when `verdict == "TRUE" and unanimous`**; `verdict` alone
  is not enough (a plurality of three is `TRUE` with one voter dissenting).
* **`unanimous` means every live voter committed and all to one verdict.** One `UNSURE` from a
  live voter on a claim makes it non-unanimous there. A voter with no vote at all in the run
  (dead, out of its free plan) is **not** counted as a voter (fixed after the first live
  run). Consequence for this ticket: expect many `UNCLEAR`, because a delta needs **two**
  decided sides. Live numbers (3 free voters, one run, the fixture at base): 57 of 80
  decided, 0 wrong; 32 of the 50 code claims. If the two sides were independent, a delta
  would be decided on about 40-50 % of the claims (the head side, which also shows the diff, may do better). The target below (≥ 90 %) is a
  hypothesis; the round's job is the machinery, and the number is recorded, not forced.
  What must be **zero** is the costly error: a `still` claim reported `FIXED` (`score_cc.py`
  calls it `still_as_fixed`). `UNCLEAR` is an honest answer, a wrong `FIXED` is not.
* **Resolved anchors.** `resolve_anchors(extract_anchors(text), view)` returns
  `ResolvedAnchor(anchor, found, path, qualname, lines, sha, candidates)`; `anchor.kind` is
  `path|symbol|test|commit|ticket|ref`. For `GONE` the "primary anchor" is the claim's first
  anchor of kind `symbol` (else `path`): `found` at base and `not found` at head, whether the
  file is still there (`found=False`, `path != ""`: CC-1's "dangling") or not.
  The fixture's two `gone` claims are `paths.norm_path` (the symbol is renamed at head; the file
  is not).
* **The fixture.** `contest-bench/cc/claims_fixture.json`: `base_sha` `d36d59e`, `head_sha`
  `5b24122`; each claim has `id`, `kind`, `truth_base`, `truth_head`, `expect`
  (`fixed` 20, `still` 10, `gone` 2, `new` 2, the rest none), `anchors`, `how`. `truth` repeats
  `truth_base`. Build the repository with `contest-bench/cc/make_fixture.py --build DIR`
  (deterministic shas). `new`: `Store.save` (false at base, true at head), `retry.retry_call`.
* **Cost of a live run.** One voter, one run, one side over the 80 fixture claims is 36
  requests (code and mixed claims two to a request, world ten); both sides 72. With three
  voters and `RUNS=3` that is about 650 requests for the fixture alone. Say it in the
  command's `--help` and print `[progress]` lines the way `ask_v2` does (one line a request,
  on stderr, flushed): a run is minutes long and the report comes at the end.
* **Voters are named as `claim_vote.py` does it:** `--models provider/model …` (Kilo's files,
  no profile needed) or `--profiles` (ini sections of `contest.local.ini`).
  **Never** put a real key, a real model name or a real hostname in a file under git:
  placeholders only in `contest.ini`.
* **A seam for the tests.** The fake voters of the tests cannot go through `ask_v2` (it
  resolves ini sections and does HTTP). `claim_diff`'s core takes the voting as a callable,
  `vote(items, side) -> list[ask_v2-shaped rows]`, and the CLI passes the real one; the tests pass
  a fake that answers from the pack text. The real one is `ask_v2` over `voters x runs`,
  nothing else.

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
| `test_unclear_when_a_voter_is_unsure_on_one_side` | one live voter `UNSURE` at head → `UNCLEAR`, not `FIXED`; `unanimous` and `verdict` both read |
| `test_dead_voter_does_not_block_a_side` | a voter with no votes at all is not counted; the other three still decide |
| `test_evidence_ids_are_read_per_side` | the same `src:` id at base and head is two entries, one per side, never "unchanged evidence" |
| `test_delta_json_is_read_by_score_cc` | `score_cc.score_deltas` on the written file: `fix_right`, `still_as_fixed`, matched by `id` |
| `test_progress_lines_on_stderr` | one `[progress]` line a request, flushed, with the fake voter |
| `test_no_network` | `socket` blocked |

## Acceptance (the operator's bench, `contest-bench/285/acceptance_285.py`)

On the fixture with the fake pack-reader voter: the 20 `fixed` claims → `FIXED` (≥ 19), the 10 `still` → `STILL` (≥ 9), the 2 `gone` → `GONE` (2), the 2 `new` → `NEW` (2), no claim
`FIXED` that the key says `still`. Then the live table of `run_cc.sh --diff` over the same
file, with three real voters of three families (the operator's bench; the entries may add
`contest-bench/285/acceptance_285.py`, the operator rewrites it), reported as three numbers:
**right** (the change equals `expect`), **unclear** (an honest `UNCLEAR`), **wrong** (any other
change). Target of `EPIC-CC.md` §6.2: ≥ 90 % right on the fix claims, `still_as_fixed` = 0. A
miss on *right* because of `UNCLEAR` is expected and recorded; a **wrong** is a fix ticket in
the 286-289 gap.

## Edge cases to handle

A head where the file moved (rename detection helps CC-4's chunk; `GONE` is for names that
vanish, a moved file with the same symbol resolves at head by CC-1's module-qualified
rule); a head that is an ancestor of base (a reverted fix: `NEW`); a claim about a commit
that exists only at head (dangling at base: `NEW`/`UNCLEAR` by the rule); `base` or `head`
the same sha twice.

## Not in scope

Running tests at either commit, judging a diff that has no claims, writing `truth.csv` (CC-8), changing the unanimity rule or the quote check (CC-6), a new LLM client or ini reader.
