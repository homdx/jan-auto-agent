# validate1 — analytics runbook

How to turn the reviewer CSVs in this folder into a scored report. All commands
run from the repo root. `python` is not on PATH — use `python3`.

The three scripts, in the order you use them:

| script | question it answers | needs |
|---|---|---|
| `scripts/merge_validations.py` | who said what, where do they split | reviewer CSVs |
| `scripts/harvest_report.py` | what do I fix, which reviewers to trust | reviewer CSVs (+ optional `truth.csv`) |
| `scripts/truth_consensus.py` | merge several human/adjudicator verdicts into one `truth.csv` | `truth-*.csv` verdict files |

Status of the audit: all three run clean on the current data. One bug in
`harvest_report.py::classify` was fixed in commit `829ffb1` (local, not pushed).

---

## 0. Clean the input set first

The folder has 11 CSVs; 4 should not be scored:

```bash
mkdir -p validate1/_excluded
git mv validate1/validation-v1-kilo.csv \
       validate1/validation-v-step-3-7-flash.csv \
       validate1/validation-v1-mistral-medium-3-5-free.csv \
       validate1/validation-v1-agnes-2-5-flash-pass1.csv \
       validate1/_excluded/
```

- `kilo` — 8/8 rows CONFIRMED, all MEDIUM. The rubric's own test: an
  all-CONFIRMED reviewer did not verify.
- `step-3-7-flash`, `mistral-medium-3-5-free` — audited `examples/hello-world`,
  not the repo under test (`main.py::module-level docstring`, `test_main.py`).
- `agnes-2-5-flash-pass1` — superseded by `agnes-2-5-flash` (same model, re-run;
  5 verdicts improved). Keeping both double-counts it as two reviewers.

Keep: `agnes-2-5-flash`, `glm`, `glm-4.5-flash`, `hy3`, `longcat-2-0-free`,
`sensenova-6.7-flash-lite`, `sensenova-6.8-flash-lite` (7 files, 7 distinct
reviewers).

---

## 1. merge_validations.py — the "who said what" view

```bash
python3 scripts/merge_validations.py validate1/validation-v*.csv
```

Pass the paths **unquoted** — this script does not expand globs itself (unlike
`harvest_report.py`). A quoted `'…*.csv'` is opened literally and errors.

Reads every CSV, groups rows by `file::symbol` (task_id as fallback), prints:

- **Reviewer behaviour** table — rows, verdict counts, `no-evidence`
  (CONFIRMED with no quoted code), `no-disproof` (never tried to falsify).
  Both should be 0; `append_finding.py` enforces it.
- **SPLIT VERDICTS** — findings where reviewers disagree. Read these first.
- **AGREED** — everyone gave the same verdict.
- **SEEN BY ONE REVIEWER** — incl. `NEW-*` discoveries.

No truth file, no scoring — this is the raw comparison.

### `--csv merged.csv` — the pivot table

```bash
python3 scripts/merge_validations.py validate1/validation-v*.csv --csv validate1/merged.csv
```

The on-screen report is scannable but you cannot sort or filter it. `merged.csv`
is a **pivot: one row per finding, one column per reviewer** holding that
reviewer's verdict. Read a row left to right to see who said what; sort by
`severity`, filter `agreement == SPLIT`. 93 findings on the current run → 93 data
rows.

| column | meaning |
|---|---|
| `finding` | grouping key: `path::symbol`, or the bare `task_id` when the row named no file. **One finding = one row.** |
| `severity` | worst severity anyone gave it (`CRITICAL > HIGH > MEDIUM > LOW > NONE`) |
| `agreement` | `SPLIT` — reviewers gave 2+ distinct verdicts (read these) · `UNANIMOUS` — everyone who looked agreed · `SOLO` — only one reviewer judged it (a `NEW-*` discovery, or a list entry nobody else reached) |
| `reviewers` | how many distinct reviewers judged this finding |
| `confirmed` / `dismissed` / `fixed` | vote tally. `dismissed` = FALSE_POSITIVE + OUT_OF_SCOPE + UNVERIFIABLE; `fixed` = ALREADY_FIXED. **These can add up to more than `reviewers`** — see the next row. |
| `task_ids` | the `IMPROVEMENTS.md` id(s) that map onto this finding. Two ids here (e.g. `AUTO-T18 AUTO-T19`) means the source list proposed the same symbol twice, so each reviewer judged it twice and the tallies are doubled. |
| `title` | the longest title any reviewer gave it |
| one column per reviewer (`glm`, `hy3`, `kilo`, …) | that reviewer's verdict, or **blank** if they never judged it. `A\|B` means they judged it twice (two task-ids) and disagreed with themselves — that is also what makes the tallies exceed `reviewers`. |

Rows are ordered `severity`, then `SPLIT` before `SOLO` before `UNANIMOUS`, then
`finding` — contested high-severity findings at the top.

> **On "why does a finding with `reviewers=8` have `dismissed=12`":** the source
> `IMPROVEMENTS.md` listed that one symbol under two task-ids (`AUTO-T18` and
> `AUTO-T19`), so all 8 reviewers judged it twice — 16 judgements, here 12
> dismiss + 2 fixed + 2 blank. The reviewer columns show the `A|B` self-splits.
> If you only want the "one clean verdict per reviewer" view, this is the
> minority case (2 findings out of 93); everywhere else `confirmed + dismissed
> + fixed == reviewers`.

The detail fields (`impact`, `evidence`, `disproof`, `repro`) are **not** in this
file — they are in `harvest.md` (step 2) and in each reviewer's own CSV.

Quick reads once it is open:

```bash
# every contested finding, aligned
awk -F, 'NR==1 || $3=="SPLIT"' validate1/merged.csv | column -t -s,

# the two findings where the tally is doubled
awk -F, 'NR==1 || $8 ~ / /' validate1/merged.csv | column -t -s,
```

---

## 2. harvest_report.py — the ranked, actionable report

### 2a. Without ground truth (what you can run now)

```bash
python3 scripts/harvest_report.py 'validate1/validation-v*.csv' \
    --report   validate1/harvest.md \
    --actions  validate1/actions.csv \
    --solo     validate1/solo.csv \
    --pending  validate1/pending-validation.md
```

- Quote the glob (`'...'`) — the script expands it itself and also accepts
  literal paths.
- Non-reviewer CSVs (a `truth.csv`, a previous `actions.csv`) are detected by
  column set and skipped with a note on stderr — safe to point it at `*.csv`.

Outputs:

| file | contents |
|---|---|
| `harvest.md` | ACT / DISPUTED / FIXED / DISMISSED buckets + solo table + reviewer scorecard |
| `actions.csv` | just the ACT rows, one line each |
| `solo.csv` | solo discoveries vs unshared judgements, kept separate |
| `pending-validation.md` | self-contained verification queue: every finding ≥1 reviewer confirmed and no human has checked, quoted in full so it needs no CSVs |

Bucket rules (`classify`): `ACT` = 2+ CONFIRMED, or a `NEW-*` solo confirm ·
`DISPUTED` = CONFIRMED vs dismissed, **or a lone non-NEW CONFIRMED** (the
`829ffb1` fix) · `FIXED` = only ALREADY_FIXED · `DISMISSED` = the noise floor.

On the current 7-file set: **ACT 2, DISPUTED 10, FIXED 7, DISMISSED 79**. The
two ACT rows are both sensenova-6.8's `NEW-*` finds
(`search_agent.py::_DEFAULT_SKIP_DIRS`, `file_reader.py::list_py_files`).

### 2b. With ground truth (after step 3)

Add `--truth validate1/truth.csv`. This fills the **accuracy** column of the
scorecard — scored only over findings a human checked, so a reviewer cannot
gain by guessing. Rank reviewers on accuracy first, then on `NEW` count.

---

## 3. Build truth.csv

The verification queue (`pending-validation.md`) lists the findings to check.
For each: open the file, decide `REAL` / `FALSE` / `FIXED`. Two ways to record:

### 3a. By hand

```csv
finding,truth,checked_by,how
tools/file_reader.py::list_py_files,REAL,renat,"path-qualified skip_dirs entry never matches os.walk dirname; dir walked anyway, no log"
tools/search_agent.py::_DEFAULT_SKIP_DIRS,REAL,renat,"module list bound by reference into self.skip_dirs; append leaks across instances"
```

Save as `validate1/truth.csv`.

### 3b. Via adjudicator models + truth_consensus.py

Run several adjudicators over the queue, each writing `validate1/truth-<name>.csv`
with columns `finding,truth,how` (plus optional `severity,consequence,evidence,fix`).
Then:

```bash
python3 scripts/truth_consensus.py 'validate1/truth-*.csv' \
    --truth   validate1/truth.csv \
    --report  validate1/adjudication.md \
    --arbiter renat        # optional: whose verdict breaks a tie
```

- Findings where all adjudicators agree → written to `truth.csv`.
- Splits → left out of `truth.csv`, listed in `adjudication.md` for a human.
  With `--arbiter NAME`, that adjudicator's vote settles the split.
- Exit code 4 if anything is left disputed (useful in a wrapper script).
- Glob must be `truth-*.csv`, not `truth*.csv` — the latter re-ingests the
  `truth.csv` output as an adjudicator named `truth`.

---

## 4. Re-run the report against truth

```bash
python3 scripts/harvest_report.py 'validate1/validation-v*.csv' \
    --truth   validate1/truth.csv \
    --report  validate1/harvest.md \
    --actions validate1/actions.csv \
    --solo    validate1/solo.csv
```

Now the scorecard's **accuracy** column is populated and the solo table marks
each finding `REAL ✅` / `FALSE ✗` / `FIXED` / `_unchecked_`.

---

## 5. Generate tickets from the ACT rows (optional)

```bash
python3 scripts/make_jira_tasks.py validate1/actions.csv --out validate1/tickets/
```

---

## Verifying the analytics themselves

```bash
python3 -m pytest tests/test_competition_harvest_classify.py -q --timeout=180
```

7 tests, covering every `classify` routing case and the `829ffb1` regression.
The suite has four roots that must be run separately — see the project note on
test roots; this file lives in `tests/` and is mirrored into `.smoke_tests/` by
`scripts/sync_test_tiers.py` (a pre-commit hook enforces the mirror).
