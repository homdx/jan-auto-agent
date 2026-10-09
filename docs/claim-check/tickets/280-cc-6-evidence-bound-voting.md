# CC-6 — evidence-bound voting: prompt v2, the quote check, and `--target`

**Status:** draft
**Severity:** HIGH (this is the ticket that makes the epic's promise)
**File:** `tools/claimcheck/judge.py`
**Symbol:** `build_prompt_v2`, `parse_votes_v2`, `verify_quotes`, `ask_with_packs`
**Round:** —
**Size:** L
**Depends on:** CC-5 (`build_pack`, `Pack.find`), CC-2 (`Target`), CC-1 (`classify`)
**Also touches:** `scripts/claim_vote.py`, `contest.ini`, `tests/test_claimcheck_judge.py`, `tests/test_claim_check.py`, `docs/claim-check/RUNBOOK.md`

Sequential. It is the largest ticket: if the round shows the voter side too big, the split
is `judge.py` first (prompt, parser, quote check, tested with fakes) and the `claim_vote.py`
wiring second, as 281 and 282 from the gap numbers.

---

## Why

With a pack in the prompt a voter *can* judge a code claim. It can also **pretend** to: say
`TRUE` because the claim sounds right, and cite nothing. The only defence that does not rely
on trusting the model is to make it quote, and to check the quote by code. A verdict the
evidence does not support becomes `UNSURE`, which costs us a pending-list entry and never a
wrong answer.

## What it does

### Prompt v2 (`build_prompt_v2(items, run, seed, fixed=False)`)

`items` is a list of `(claim, pack_or_None)`. The prompt keeps `CLAIM-1`'s run variation
(three openers with the first sentence's words rearranged, a shuffled claim order,
`fixed=True` as the control) and adds, **per claim**, its rendered pack (`Pack.render()`).
System rules, verbatim in the code and covered by a test:

> For a claim about this repository, the evidence under it is your only source: do not use
> what you remember about similar code. For a fact about the world you may use what you
> know. If the evidence does not decide the claim, answer UNSURE — that is a correct answer.
> For TRUE or FALSE, name the chunk (`[[id]]`) and copy, character for character, up to 200
> characters from that chunk that decide the claim.

Reply format, JSON only:
`[{"id": <n>, "verdict": "TRUE|FALSE|UNSURE", "chunk": "<id>", "quote": "<text>"}]`.

World claims go in the same prompt with no pack and need no chunk or quote.

### Parser (`parse_votes_v2(text, order) -> dict[int, Vote]`)

`Vote(verdict, chunk, quote)`. Tolerates code fences, prose around the JSON, a trailing
comma, `verdict` in any case, `id` as a string. A row with an unknown verdict or an `id`
out of range is dropped. Garbage gives `{}` (the model counts as having cast no votes, as
in `CLAIM-1`).

### The quote check (`verify_quotes(votes, packs) -> (votes, rejected)`)

For each `TRUE`/`FALSE` on a claim that has a pack:

* the `chunk` must be an id in **that claim's** pack, **and**
* `pack.find(quote)` must return **that same id**.

Otherwise the vote becomes `UNSURE` and is listed in `rejected` with the reason
`chunk_not_in_pack` or `quote_not_in_pack`. A world claim (no pack) is exempt. A dangling
claim's `FALSE` is valid when its quote is from the `note:dangling:…` chunk. `rejected`
is written into `votes.json` per voter and per run, so the rate of fabrication is a number,
not a feeling.

### `claim_vote.py`

* New options: `--target REPO`, `--ref REF`, `--base REF`, `--fetch`, `--pack-chars N`.
  Without `--target` the script behaves exactly as at `03a77c5` (the `test_claim_check.py`
  tests stay green unchanged).
* With `--target`: open `Target(repo, ref, fetch=…, expect_sha=<the report's hint if
  given>)`, build the `PathRepoView`/`Target.view()`, extract and resolve anchors for every
  claim (CC-1), `classify`; `world` claims go as today; `code` and `mixed` claims get a pack
  (`build_pack`, with `base`/`head` when `--base` is given: `head` is the target's sha).
* **Batching.** World claims keep `[claim_vote] batch`. Code and mixed claims go in batches
  of `[claim_vote] code_batch` (default 2) so a prompt stays small.
* `votes.json` per claim gains `kind`, `dangling`, `sha`, `evidence` (the chunk ids the
  accepted votes cited), `quotes` (the accepted quotes), `rejected` (counts by reason).
  `needs_code` is kept for compatibility. `tally` is **unchanged** in its rule (UNSURE
  abstains, quorum 3, `unanimous`); it receives the votes already verified.
* The console line gains the totals: `rejected quotes: N`, `warnings: …` (from the target).

### `contest.ini`

`[claim_vote]` gains `code_batch = 2`, `pack_chars = 6000`, `pack_chunks = 6`,
`pack_chunk_chars = 2400`, each with a one-line comment; placeholders only.

## Tests

`tests/test_claimcheck_judge.py` (fake `completion_fn`, no network, no real model):

| Test | What it pins |
|---|---|
| `test_prompt_carries_each_claims_pack_and_the_rules` | the rules text, each pack under its claim, ids in the order shown |
| `test_prompt_variation_is_kept` | three runs: different first sentence and order; `fixed=True`: identical bytes |
| `test_world_claim_has_no_pack_and_no_quote_needed` | the prompt block and the check |
| `test_parse_tolerates_fences_prose_trailing_comma` | table of 12 replies |
| `test_parse_drops_unknown_verdicts_and_bad_ids` | |
| `test_quote_in_the_named_chunk_is_accepted` | exact; whitespace-different; with `NNN|` gutter pasted |
| `test_quote_from_another_chunk_is_downgraded` | `chunk_not_in_pack` or `quote_not_in_pack` as specified |
| `test_fabricated_quote_is_downgraded` | and counted in `rejected` |
| `test_short_quote_is_downgraded` | under 8 characters |
| `test_dangling_false_with_the_note_quote_is_accepted` | |
| `test_unsure_needs_no_quote` | |
| `test_end_to_end_with_fake_voters` | three fakes that answer from the pack text (`TRUE` iff the pack contains `check=False`): all quote correctly → `unanimous`, `evidence` filled; one fabricates → not unanimous, `rejected` = 1; one answers a non-existent chunk → same |
| `test_no_network` | `socket` blocked for the whole module |

`tests/test_claim_check.py` (extended): `test_claim_vote_without_target_is_unchanged` pins
the `CLAIM-1` behaviour; `test_target_options_parse`; `test_votes_json_has_the_new_fields`;
`test_code_batch_splits_code_claims` (a recorded call log shows code claims two at a time and
world claims ten); `test_report_sha_mismatch_is_printed`.

## Acceptance (the operator's bench, `contest-bench/280/acceptance_280.py` and `contest-bench/cc/run_cc.sh`)

1. **Offline:** the fake-voter end-to-end over the fixture's 80 claims — the chain
   classifies, packs, prompts, parses, checks quotes and tallies without an exception, with
   a deterministic fake that answers from the pack (a perfect pack-reader and a lazy
   "always TRUE" fake): the perfect fake scores ≥ 95 % on the fixture, the lazy one is
   caught by the quote check on ≥ 95 % of its code verdicts.
2. **Live (the operator, not pytest):** `run_cc.sh --target` with three voters of three
   families over the fixture and over the 30 real claims; the table is compared with
   `BASELINE.md` and with the targets of `EPIC-CC.md` §6.2. The numbers are recorded in the
   round's `SUMMARY.md`; a missed target opens a fix ticket in the 281–284 gap.

## Edge cases to handle

A claim whose pack is empty (voters must say `UNSURE`; a `TRUE` with no quote is
downgraded); a reply that quotes a chunk id with different brackets; a quote containing
a literal `|`; a pack that was trimmed so the deciding line is gone (the voter says
`UNSURE`, correct); a model that returns the claim text as the quote; 4 voters of which two
are the same family (unchanged: `unanimous` needs all four to agree).

## Not in scope

Before/after (CC-7), `truth.csv` (CC-8), changing the unanimity rule, executing code.
