# Running the whole round, end to end

One page for the operator. Five stages, each one: **what you hand the model,
what it runs, what you take away.** Per-stage detail lives in the prompt files
named below; this file is the map and the handover list.

All paths are from the repo root. `python` is not on PATH — use `python3`.

---

## The map

| # | stage | prompt you hand the model | scripts it runs | you take away |
|---|---|---|---|---|
| 1 | **Propose** | `docs/TASK-jan-selfaudit.md` | `main.py --auto … --dry-run`, then `--validate-plan` | `IMPROVEMENTS.md` |
| 2 | **Review** | `VALIDATE-jan-findings.md` | `next_finding.py` → `append_finding.py` | `validate<N>/validation-v<V>-<model>.csv`, one per model |
| — | *analyse* | — | `merge_validations.py`, `harvest_report.py`, `gui_report_merged_csv.py` | `merged.csv`, `harvest.md`, `actions.csv`, `pending-validation.md` |
| 3 | **Adjudicate** | `docs/ADJUDICATE-findings.md` | `next_pending.py` → `append_verdict.py` | `validate<N>/truth-<name>.csv` → `truth.csv` |
| 4 | **Ticket** | — (you run it) | `truth_consensus.py`, `make_jira_tasks.py` | `tasks/NN-*.md` + `tasks/INDEX.md` |
| 5 | **Fix** | `docs/FIX-round.md` | `next_task.py` → `append_task.py` | commits + `PROGRESS.csv` |
| — | *rate* | — | `harvest_report.py --truth`, `gui_report_merged_csv.py --truth` | the scorecard |

Stages 2, 3 and 5 all use the same mechanic: **the model is handed one item at
a time, and the queue is derived from its own output file on disk.** It cannot
batch, cannot skip ahead, and a context blow-up is a resume, not a data loss.
Re-run the same command and it continues.

---

## Stage 0 — running against a different repository

The harness is repo-agnostic; only stage 1 knows anything about jan. Copy into
the target repo:

```
scripts/next_finding.py  scripts/append_finding.py     # stage 2
scripts/next_pending.py  scripts/append_verdict.py     # stage 3
scripts/merge_validations.py  scripts/harvest_report.py
scripts/gui_report_merged_csv.py  scripts/truth_consensus.py
scripts/next_task.py  scripts/append_task.py  scripts/make_jira_tasks.py
VALIDATE-jan-findings.md  docs/ADJUDICATE-findings.md  docs/FIX-round.md
```

Two things to edit before the first run:

- **the ground rules** in `docs/FIX-round.md` §Ground rules — test roots, the
  `python3` note and the `sync_test_tiers.py` mirror are jan-specific;
- **`gui_report_merged_csv.py`'s baked-in `GROUND` table** — or just never use
  it and always pass `--truth <your truth.csv>` (see [Rating](#rating-the-models)).

The finding list itself (`IMPROVEMENTS.md`) does not have to come from jan. Any
list of `### <ID>: <title>` sections with a `**Location:**` and an
`**Instruction:**` works — that is the whole contract `next_finding.py` reads.

---

## Stage 1 — jan proposes

You run this, not a reviewer model. Full detail and the six goal variants are in
`docs/TASK-jan-selfaudit.md`; variant 1 is a known-answer calibration, run it
first.

```bash
python3 main.py --auto "<GOAL>" --dry-run --config agents_128k.ini --base .
python3 main.py --validate-plan --config agents_128k.ini --base .
cp IMPROVEMENTS.md IMPROVEMENTS-$(date +%H%M)-<variant>.md     # each run overwrites
```

**Take away:** `IMPROVEMENTS.md`. Check it before handing it on —
`--auto` has produced heading-only files where every entry lost its
`**Location:**` and `**Instruction:**`. `next_finding.py` refuses such a file
outright, but check anyway:

```bash
grep -c '^### ' IMPROVEMENTS.md ; grep -c '\*\*Instruction:\*\*' IMPROVEMENTS.md
```

Those two numbers must match. If they do not, the reviewers will be judging
titles, and 88% of what comes back will be noise.

---

## Stage 2 — the reviewer models

**Hand each model:** the repo (read-only is fine — reviewers do not commit),
`VALIDATE-jan-findings.md`, and `IMPROVEMENTS.md`. That is all; the two scripts
are already in the repo and the prompt tells the model to run them.

The prompt is the file itself. Send it verbatim, plus one line:

> You are reviewing variant `<N>`. Use `--reviewer <your model name, lowercase>`
> on every `append_finding.py` call.

Each model loops on its own:

```bash
python3 scripts/next_finding.py --improvements IMPROVEMENTS.md \
                                --out validate1/validation-v1-<model>.csv
# judge that one entry, then:
python3 scripts/append_finding.py --out validate1/validation-v1-<model>.csv --variant 1 \
    --task-id <id> --title "..." --file <real path> --symbol <Class.method> \
    --verdict CONFIRMED --severity MEDIUM --evidence "..." --disproof "..."
```

**Take away:** one CSV per model in `validate<N>/`. Nothing else — a model that
wrote prose but no CSV rows did nothing.

**Before scoring, drop the files that cannot be scored.** Three tells, all
visible in the `merge_validations.py` reviewer table:

| tell | meaning |
|---|---|
| every verdict is `CONFIRMED` | it did not verify. Exclude. |
| paths point outside the repo (`examples/hello-world/…`) | it audited the wrong tree. Exclude. |
| two files for one model (a re-run) | keep the better one only, or it counts as two reviewers. |

Move them out of the glob (`validate1/_excluded/`) rather than deleting them.

---

## Analysing stage 2

Three views, increasing in commitment. All are CSV-only — no source needed.

```bash
# who said what, and where they split
python3 scripts/merge_validations.py validate1/validation-v*.csv \
    --csv validate1/merged.csv

# what to act on, and the reviewer scorecard
python3 scripts/harvest_report.py 'validate1/validation-v*.csv' \
    --report  validate1/harvest.md   --actions validate1/actions.csv \
    --solo    validate1/solo.csv     --pending validate1/pending-validation.md

# the colour console view of merged.csv
python3 scripts/gui_report_merged_csv.py validate1/merged.csv
```

Note the quoting difference: `harvest_report.py` expands its own glob, so quote
it; `merge_validations.py` does not, so leave it to the shell.

`merged.csv` is a **pivot** — one row per finding, one column per reviewer.
`harvest.md` buckets everything into ACT / DISPUTED / FIXED / DISMISSED. Full
column-by-column reference: `validate1/ANALYTICS-RUNBOOK.md`.

**The GUI script** (`gui_report_merged_csv.py`) is the one to open first — it
renders `merged.csv` as a coloured matrix, one row per finding, one column per
model, plus a verdict-distribution histogram. Its last two sections only appear
when it has ground truth:

- **GROUND TRUTH ACCURACY** — correct / false-positive / false-negative counts
  and an accuracy percentage per model, side by side;
- **GROUND TRUTH ROW DETAIL** — each settled finding as a row, with every
  model's cell coloured by whether it got that one right.

Where that ground truth comes from matters. By default it is a **10-entry table
baked into the script** — a frozen snapshot that does not grow when your
`truth.csv` does. After stage 3, always pass the real thing:

```bash
python3 scripts/gui_report_merged_csv.py validate1/merged.csv --truth validate1/truth.csv
```

The section header prints which source it used, so you can always tell. Rows
carrying a `duplicate_of` are skipped — one defect, one score.

---

## Stage 3 — adjudication

`harvest.md` cannot tell a real defect from a confident mistake; only reading
the code can. `pending-validation.md` is the queue: every finding at least one
reviewer confirmed that nobody has checked. It quotes each finding in full, so
it travels on its own.

**Hand each adjudicator:** the repo (it must read live code), the queue file,
and `docs/ADJUDICATE-findings.md`.

```bash
python3 scripts/next_pending.py --pending validate1/pending-validation.md \
                                --out validate1/truth-<name>.csv
python3 scripts/append_verdict.py --out validate1/truth-<name>.csv \
    --finding "<file>::<symbol>" --truth REAL|FALSE|FIXED --how "..." --evidence "..."
```

`FALSE` is the verdict that matters here — it is the one an automated reviewer
structurally cannot reach, because it requires proving a negative against the
live code.

Merge the adjudicators into one file:

```bash
python3 scripts/truth_consensus.py 'validate1/truth-*.csv' \
    --truth validate1/truth.csv --report validate1/adjudication.md \
    --arbiter <who breaks ties>
```

Glob `truth-*.csv`, never `truth*.csv` — the latter re-ingests the output as an
adjudicator named `truth`. Exit code 4 means something is still split.

**Take away:** `validate<N>/truth.csv` — `finding,truth,checked_by,duplicate_of,how`.
This is the only artefact of the whole round that is worth keeping long term.

---

## Stage 4 — turn truth into tickets

You run this; no model involved.

```bash
python3 scripts/make_jira_tasks.py --truth validate1/truth.csv \
    --findings validate1/validation-v*.csv \
    --verdicts validate1/truth-*.csv \
    --out tasks/
```

`--findings` and `--verdicts` are optional but worth passing: they fill each
ticket's evidence, reproduction and disproof sections from what the reviewers
and adjudicators actually wrote, instead of only `truth.csv`'s one-line `how`.

**Take away:** `tasks/NN-*.md` (one self-contained ticket per confirmed defect)
and `tasks/INDEX.md`. Read the index before starting stage 5 — a ticket with
`Severity: NONE` and "latent only" in its text is asking a coder to change
working code, and is usually better dropped from the round.

---

## Stage 5 — the coding round

Full instructions, both the solo and the several-models-at-once handout:
**`docs/FIX-round.md`**. In short: hand the coder the repo on its own branch,
that file, and `tasks/`; it runs `next_task.py` → fix → commit →
`append_task.py` until exit code 3. Nothing from `validate<N>/` is needed during
the round.

---

## Rating the models

Two scorecards, and they answer different questions.

```bash
# stage 2: which reviewer is worth keeping
python3 scripts/harvest_report.py 'validate1/validation-v*.csv' \
    --truth validate1/truth.csv --report validate1/harvest.md \
    --actions validate1/actions.csv --solo validate1/solo.csv

# the same data as a side-by-side matrix
python3 scripts/gui_report_merged_csv.py validate1/merged.csv --truth validate1/truth.csv
```

Read the scorecard in this order:

1. **NEW** — findings the model raised that were not on the list. This is the
   only column that measures whether it read the code rather than the titles.
   A round where the list is mostly noise is won here.
2. **accuracy** — scored only over findings in `--truth`, so it cannot be gained
   by guessing about unchecked ones. Watch the denominator: a model whose rows
   name paths nobody else used gets a small one and a meaningless percentage.
3. **skepticism** — share rejected. Near 100% with zero NEW is not judgement,
   it is a model dismissing a list it never opened; near 0% is the same failure
   in the other direction.
4. **rows** — coverage. A 93% accuracy over 40 of 53 entries is not comparable
   with 93% over all 53; say which when you report it.

`solo.csv` splits "only this model saw it" into **discoveries** (`NEW-*`) and
**unshared judgements** (list entries nobody else reached). Only the first is
credit.

For stage 5, rank on: tickets correctly recorded `ALREADY-OK` (it verified
before patching), regression tests that actually fail without the change, and
commits that stayed in scope.

---

## What to keep

| keep | it is |
|---|---|
| `validate<N>/truth.csv` | the settled answer; the input to every later round |
| `GROUND-competition.md` | the carried-forward summary |
| `tasks/` + the fix commits | the work |
| `IMPROVEMENTS-<time>-<variant>.md` | needed to reproduce a round |

Everything else — reviewer CSVs, `merged.csv`, `harvest.md`, `actions.csv`,
`solo.csv`, `pending-validation.md`, `PROGRESS.csv` — is **run data**. It is
gitignored on purpose: it is regenerable from the CSVs, and committing it makes
every re-run a diff.
