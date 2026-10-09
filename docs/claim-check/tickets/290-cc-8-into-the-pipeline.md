# CC-8 — into the pipeline: `truth.csv`, the pending list, and one command

**Status:** draft
**Severity:** MEDIUM (without it the result stays a table nobody feeds forward)
**File:** `tools/claimcheck/truth.py`
**Symbol:** `to_truth_rows`, `finding_key`, `pending_markdown`, `write_truth`
**Round:** —
**Size:** M
**Depends on:** CC-6 (`votes.json` with evidence)
**Also touches:** `scripts/claimcheck_truth.py`, `scripts/run_claim_check.sh`, `docs/claim-check/RUNBOOK.md`, `docs/PIPELINE-DOCS-INDEX.md`, `tests/test_claimcheck_truth.py`

Runs in parallel with CC-7: CC-8 owns `truth.py`, `claimcheck_truth.py`, the edits to
`run_claim_check.sh`, `RUNBOOK.md` and the docs index; CC-7 owns `compare.py`, `claim_diff.py`
and `BEFORE-AFTER.md`. `RUNBOOK.md` gets **one line** pointing to `BEFORE-AFTER.md` from this
ticket (the page itself is CC-7's).

---

## Why

The competition runbook already has the machinery for settled answers: `truth.csv` is "the
one artefact worth keeping", `harvest_report.py --truth` scores reviewers against it,
`truth_consensus.py` merges adjudicators, stage 3 reads a pending list. This epic's output is
exactly "claims with a settled verdict and the evidence for it" and "claims nobody could
settle". It should land in those files, not in a new format.

## What it does

### `to_truth_rows(votes, *, checked_by) -> list[dict]`

For each claim of a `votes.json` (CC-6's shape):

| `votes.json` | row |
|---|---|
| `unanimous` and verdict `TRUE`, `needs_code` or not | `truth=REAL` |
| `unanimous` and verdict `FALSE` | `truth=FALSE` |
| anything else (`SPLIT`, `UNSURE`, `CODE-CHECK`, not unanimous) | **no row**; it goes to the pending list |

Columns `finding,truth,checked_by,duplicate_of,how`: the header `validate1/truth.csv` has carried since the stage-4 prep; `validate1/ANALYTICS-RUNBOOK.md` names `finding,truth,how` as the minimum and lists the rest as optional, and `harvest_report.load_truth` reads by column name, so the extra columns are safe (pinned by a test).

* `finding`: `finding_key(claim, resolved)` — `<path>::<qualname>` of the claim's primary
  resolved symbol anchor (the grouping key `harvest_report.py` and `merge_validations.py`
  already use), else `<path>` of its primary path anchor, else `claim:<sha1[:8]>` of the
  normalised claim text.
* `checked_by`: `claimcheck:<voter family names, sorted, joined by +>@<sha7>` (and
  `@<base7>..<head7>` for a before/after run).
* `how`: one line — the accepted chunk ids and the first accepted quote, cut at 160
  characters, with `;` and quotes escaped for CSV.
* `duplicate_of`: when two claims map to the same `finding` with the same `truth`, the
  second row's `duplicate_of` is the first's `finding`; the first is empty.

### `pending_markdown(votes) -> str`

The stage-3 queue for what was not settled: for each claim, its kind, the voters'
verdicts (a small table by model), the rejected-quote count, the claim, and the **pack's
chunk ids with the first lines of each** — so the adjudicator opens the right place
instead of searching. Grouped: `SPLIT` first, then `CODE-CHECK` (no evidence found), then
`UNSURE`. A header line states the sha and the voters.

### `scripts/claimcheck_truth.py votes.json --out truth.csv --pending pending.md [--append] [--checked-by TEXT]`

Writes the two files. `--append` adds only rows whose `finding` is not already in the
file with the same `truth` (idempotent: running it twice appends nothing); a **different**
`truth` for an existing `finding` is not overwritten, it is printed as a conflict and the
exit code is 3 (the `truth_consensus.py` convention: "a split exits 4" is its own; here
3 means "an existing settled row disagrees"). Exit 0 otherwise. The CSV is written
atomically (temp file, rename).

### `scripts/run_claim_check.sh`

* New options, passed through: `--target REPO`, `--ref REF`, `--base REF`, `--fetch`, and
  `--truth OUT.csv` to run `claimcheck_truth.py` at the end into the same output directory.
* `--dry-run` prints the commands it would run and exits 0, without any network or file
  written (this is what the test uses).
* Output directory unchanged (`claim-check-out/<UTC>/`) plus `truth.csv` and `pending.md`.
* With no new option it behaves as at `03a77c5`.

### Docs

`RUNBOOK.md`: a "With the code in front of the voters" section (the three new options, the
acceptance rule restated, where `truth.csv` and `pending.md` go, how they plug into stage 3
and `harvest_report.py --truth`) and the one-line pointer to `BEFORE-AFTER.md`.
`docs/PIPELINE-DOCS-INDEX.md`: one row for `docs/claim-check/RUNBOOK.md` and one for
`EPIC-CC.md`.

## Tests (`tests/test_claimcheck_truth.py`)

| Test | What it pins |
|---|---|
| `test_rows_only_for_unanimous_verdicts` | table of verdict kinds → rows or none |
| `test_truth_values` | `TRUE`→`REAL`, `FALSE`→`FALSE` |
| `test_finding_key_forms` | symbol → `path::qualname`; path only → `path`; neither → `claim:<8 hex>`; stable across runs |
| `test_header_columns` | the header is exactly `finding,truth,checked_by,duplicate_of,how`, and a file of only `finding,truth,how` is still read by `harvest_report.load_truth` |
| `test_checked_by_names_families_and_sha` | including the before/after form |
| `test_how_is_one_csv_safe_line` | quotes, commas, newlines, a 400-char quote cut at 160 |
| `test_duplicate_of` | two claims, one finding, same truth → the second points at the first |
| `test_append_is_idempotent` | twice → file unchanged the second time |
| `test_append_conflict_exits_3_and_keeps_the_old_row` | |
| `test_atomic_write` | a failure while writing leaves the old file intact |
| `test_pending_lists_unsettled_with_chunk_ids` | order SPLIT, CODE-CHECK, UNSURE; the chunk ids and first lines present |
| `test_harvest_report_reads_the_csv` | `scripts/harvest_report.load_truth(path)` (imported, called on the CSV; no subprocess) returns every row, with `REAL`/`FALSE` as written |
| `test_truth_consensus_reads_the_csv` | likewise for `truth_consensus.py` |
| `test_run_claim_check_dry_run` | `bash scripts/run_claim_check.sh r.md --target . --ref HEAD --truth t.csv --dry-run` prints the extract, vote and truth commands with the options; writes nothing; exit 0 |
| `test_run_claim_check_old_form_unchanged` | with no new option the dry run equals the `03a77c5` command list |

## Acceptance (the operator's bench, `contest-bench/290/acceptance_290.py`)

From the live run of `run_cc.sh` over the fixture (CC-6's votes): `truth.csv` has a row for
every unanimous claim and none for any other; **against the fixture's key** the `truth`
column is right on ≥ 97 % of its rows (the figure that makes `harvest_report.py --truth`
safe to run on it); `pending.md` lists every non-unanimous claim exactly once, each with at
least one chunk id when a pack existed; `harvest_report.py <csvs> --truth truth.csv --report`
runs to completion on it.

## Edge cases to handle

A `votes.json` from a run with no `--target` (no `evidence`: rows are still written for
unanimous world claims, `how` says `world fact`); a claim containing a newline; a
`finding` containing a comma; an empty `votes.json` (writes the header only); a pending list
for 500 claims (stays readable: chunk previews cut at 3 lines).

## Not in scope

Changing `harvest_report.py` or `truth_consensus.py`, merging with other adjudicators'
rows (their tool does that), pushing or committing anything.
