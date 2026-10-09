# Claim check — proof-of-concept report (2026-10-07)

Question: can the claims of a model-written report be checked by extracting them
for free and letting free models vote, instead of spending Lenz's 100 monthly
credits?

Answer: **for facts about the world, yes** — three free models from three families,
accepted only when unanimous, were wrong on 0.4 % of the claims they decided.
**For our round reports, the vote resolves almost nothing**, because almost every
claim in them is about our own code, which the models cannot see; there the
runbook's job is to *sort* claims, and the next step is to give the models the code.

## What Lenz gives and costs (measured against the billing page)

| Call | Cost | Limit |
|---|---|---|
| `/extract` | free | 1000 a day |
| `/assess` | 1 credit per claim; an `Error` row is not charged | 100 a month + 400 extra |
| `/verify` | 10 credits | same pool |

Credits spent in the whole experiment: **35** (1 + 14 + 6 + 10 + 4), all before the
voting runs; the extract steps and every re-run after them cost none (cache,
checked offline with the network stubbed).  `/extract` on a whole report returns one
claim; on a `##`/`###` section it returns 4–10 self-contained claims.  Cut at
sentences instead, the claims lost their referents ("that", "it") and `/assess`
answered `Mixed/low` or `Error` — 6 of 8 paid verdicts wasted.  A report with no
headings is cut into ~1800-character chunks.

## The models

40 claims with a known answer (20 true, 20 false: Python, pytest, git, HTTP, POSIX),
3 runs each, claims in batches of 20 (a list of 59 in one request returned nothing: a
reasoning model spends its budget first), calls paced to 2 in flight and one start per
4 s per host (a first run without pacing lost most answers to HTTP 429 `rpm exhausted`
and was discarded).

| Model | answered | 3 runs agree | right (majority) |
|---|---|---|---|
| sensenova-6.8-flash-lite | 40 | 20 of 20* | 39 |
| sensenova-6.7-flash-lite | 40 | 38 of 40 | 40 |
| agnes-2-0-flash | 40 | 36 of 40 | 37 |
| agnes-2-5-flash | 40 | 35 of 40 | 37 |
| agnes-3-0-flash | 40 | 35 of 40 | 39 |
| nemotron-3-super-120b-a12b | 40 | 37 of 40 | 40 |
| nemotron-3-ultra-550b-a55b | 40 | 40 of 40 | 39 |
| laguna-s-2-1 | 40 (HTTP 503 on some calls) | n/a | 37 |
| glm-4-7-flash | 20 | 19 of 20 | 19 |

\* one run of that model returned half its batches without `content`.

Not usable on 2026-10-07: `mistral-medium-3-5`, `mimo-v2-5`, `nemotron-3-5-lightning`,
`nex-n2-5-pro` (HTTP 400 model_not_found), `muse-spark-1-2-contributor` (400, no price),
`hy3` and `step-3-7-flash` (HTTP 200, no parsable verdicts).

### Does one model answer the same when asked again?

Sensenova, 40 claims, 6 runs each:

| | identical in all runs | single-call accuracy |
|---|---|---|
| 6.8, byte-identical prompt | 40 of 40 | 100 % |
| 6.7, byte-identical prompt | 38 of 40 (the two it flips on: the `--numstat` rename form and "pytest exit code 2") | 99 % |
| 6.8, reworded + shuffled | 20 of 20 compared | 99 % |
| 6.7, reworded + shuffled | 20 of 20 compared | 99 % |

(The reworded runs had several half-failed batches, so only claims with at least 5
full runs were compared.)  Rewording changes nothing, and asking again adds almost
nothing: three calls of one model are a stability check, not three opinions.

### How many models, and which rule

All subsets of the 8 models that answered every claim (4 families), per panel, of 40:

| rule | right | wrong | undecided | wrong of decided |
|---|---|---|---|---|
| 1 model | 36.3 | 1.33 | 2.3 | 3.5 % |
| 3 models, majority | 38.5 | 0.82 | 0.7 | 2.1 % |
| 9 models (all), majority | 38.0 | 0.00 | 2.0 | 0 % |
| 3 families, majority | 39.0 | 1.00 | 0 | 2.5 % |
| 3 families, **unanimous** | 36.6 | **0.14** | 3.2 | **0.4 %** |
| 4 families, unanimous | 36.0 | 0.08 | 3.9 | 0.2 % |

Majorities improve slowly with more models; **unanimity across different families
is what makes the answer trustworthy**, at the price of ~8 % of the claims going
undecided.  Minimum panel: **3 models from 3 families, unanimous.**  The claims that
beat the models are the same two everywhere ("pytest exit code 2 is a usage error" —
it is 4 — and the `--numstat` rename form): a shared mistake is shared by every
vote, which is why families matter more than count.
Earlier numbers in this work (a 20-claim run: "more models do not help") came from
partial data and are superseded by this table.

Sample caveat: 40 claims, 4 families; the 0.14 is "rarely seen", not "impossible".

## The real round reports

Five reports, extracted by section and voted by 3 models from 3 families, 3 runs each
(`kc-bug-report.md`, `review1.txt`, `review2.txt`, `review3.txt`, `kc-branch-review.md`):

| report | claims | claims where at least one model committed |
|---|---|---|
| `kc-bug-report.md` (earlier 5-model run) | 59 | 9 (2 `TRUE` after the quorum rule) |
| `review1.txt` | 2 | 0 |
| `review2.txt` | 54 | 10 |
| `review3.txt` | 66 | 10 |
| `kc-branch-review.md` | 35 | 9 |

Of the 157 claims in the last four, **128 got no committed vote from any model**, 27
one, 2 two, none three: no claim reached a unanimous verdict.  The votes the models did
cast (about 110 `TRUE`, 6 `FALSE` against ~1200 `UNSURE`) are on claims that sound
plausible about code they cannot see; 86 of the 157 are marked `needs_code`, and the
marker is a regex heuristic that leaks.  A TRUE on such a claim is a guess, not a
check, so the runbook never accepts it.

Conclusion for round reports: the cheap pre-pass is not a judge here.  What it gives
is a sorted list — public facts (a handful, checked) versus code claims (the rest, to
read or run).

## Lenz on the same material (4 paid `/assess`)

Two verdicts matched what is checkable in a second (pytest exit 4: `True/high`;
numstat rename: `Mostly True/medium`), two were `Mixed/low` (they needed our code).
The free models matched Lenz on both checkable claims and called the code claims
unknown.  Lenz's own value — sourced citations — is not needed for this use; keep
`/assess` / `/verify` as a rare tie-break (`scripts/lenz_claim_filter.py --verify`).

## What follows

1. **Give the voters the code.**  The `CODE-CHECK` rows are the work.  A voter that
   gets the file the claim names (after a `git pull` of the target repo), the way
   stage 3 adjudicators read live code, can judge them; the same unanimity rule then
   applies.  Not built yet.
2. Run the runbook on two or three more rounds and compare the `TRUE`/`FALSE` rows with
   `truth.csv` before using it as a gate.
3. Open defects: the `needs_code` regex leaks; `claim_extract.py` on a plain-text
   review returned 2 claims for 5 sections (the extractor wants prose with claims).

## Files

`scripts/claim_extract.py`, `scripts/claim_vote.py`, `scripts/run_claim_check.sh`,
`scripts/lenz_claim_filter.py` (the paid tie-break and shared helpers),
`tests/test_claim_check.py` (+ its tier link), `docs/claim-check/RUNBOOK.md`,
a `[claim_vote]` section in `contest.ini`, `claim-check-out/` in `.gitignore`.
Nothing is committed.
