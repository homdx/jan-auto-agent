# Competition docs — index

Short catalog of every markdown file in the bug-hunt competition. For *how to
run a round* see `docs/RUN-THE-COMPETITION.md` (the operator map); this page is
just "which file is what".

The round has 5 stages. Each stage has one prompt file you hand the model.

## Stage prompts

| Stage | File | What it is |
|---|---|---|
| 1 — Propose | `docs/TASK-jan-selfaudit.md` | The 6 `--auto` **goal variants** jan runs against its own source. Variant 1 (mutable state) is a known-answer calibration; variants 2–6 have never been run. Output: `IMPROVEMENTS.md`. |
| 2 — Review | `VALIDATE-jan-findings.md` | The reviewer prompt: core block + one `### Variant N` block + CSV schema + how to rank models. Reviewers judge each proposal one at a time. Output: `validate<N>/validation-v<V>-<model>.csv`. |
| 3 — Adjudicate | `docs/ADJUDICATE-findings.md` | The adjudicator prompt: settle every finding a reviewer confirmed and nobody checked, by reading live code. Output: `validate<N>/truth-<name>.csv` → merged `truth.csv`. |
| 4 — Ticket | *(no prompt — operator runs `make_jira_tasks.py`)* | Turns `truth.csv` into `tasks/NN-*.md` + `tasks/INDEX.md`. |
| 5 — Fix | `docs/FIX-round.md` | The coder prompt: the `next_task.py` → fix → commit → `append_task.py` loop, plus the run's ground rules. Solo and parallel-model variants. |

## Reference / carried forward

| File | What it is |
|---|---|
| `docs/RUN-THE-COMPETITION.md` | Operator map — all 5 stages as "hand over / run / take away", plus a full copy-paste command list and how to run against another repo. |
| `GROUND-competition.md` | Session-handoff summary: what the harness is, current results, the verified **ground-truth table** (the scoring key for every round), known traps. Read first. |
| `validate1/ANALYTICS-RUNBOOK.md` | Deep reference for the analysis step — every column of `merged.csv` / `harvest.md`, and the `merge` / `harvest` / `truth_consensus` scripts. |
| `docs/JIRA-FIX3-pullv3.md` | The single ticket used by the earlier 11-model FIX-3 round (the "everyone confirmed a false premise" experiment). Not part of the current loop. |

## Per-round outputs (regenerated each run; gitignored)

Everything under `validate<N>/` except `truth.csv` and `ANALYTICS-RUNBOOK.md` is
run data — regenerable from the CSVs, not committed.

| File | Written by | What it is |
|---|---|---|
| `validate<N>/IMPROVEMENTS.md` | stage 1 | The proposal list the reviewers judged that round. |
| `validate<N>/merged.csv` | `merge_validations.py` | Pivot: one row per finding, one column per reviewer. |
| `validate<N>/harvest.md` / `harvest-report.md` | `harvest_report.py` | Findings bucketed ACT / DISPUTED / FIXED / DISMISSED + reviewer scorecard. |
| `validate<N>/pending-validation.md` | `harvest_report.py --pending` | The stage-3 queue: each unsettled finding quoted in full. |
| `validate<N>/adjudication.md` | `truth_consensus.py` | Which findings the adjudicators agreed on, which split. |
| `validate<N>/truth.csv` | stage 3 | **The settled answer.** The one artefact worth keeping long term; copied forward into each new round. |
| `validate<N>/STAGE-STATUS.md` | by hand | Snapshot of where a given round stands. |

## JIRA tickets

The stage-4 output (`tasks/` — 5 tickets + `INDEX.md`) is **no longer carried in
the repo**. The `tasks/` folder and its `.gitignore` entries were removed once
the two real defects were fixed directly. The working copy — plus `VALIDATION.md`,
a re-check of all 5 against live code — lives at `~/qwen25-jira-tickets/`.

Outcome of that validation:

- **fixed directly on `competition`:** `03` `_DEFAULT_SKIP_DIRS` aliasing
  (`search_agent.py`, commit `8fcfe71`) and `05` `ArchProbe.last_by_op` shallow
  copy (`arch_probe.py`, commit `6c06170`) — one line each, both ground-truth REAL.
- **left for the operator:** `02` (`list_py_files` / `list_source_files` silently
  ignore a path-qualified `skip_dirs` entry) — real but needs a warn-vs-normalise
  decision, not a mechanical fix.
- **dropped:** `01` (`AutoController.config` — latent, impact disproved in
  adjudication) and `04` (`_serialise_candidates` — `Severity: NONE`).

If a future round needs tickets again, regenerate them outside the tree:
`scripts/make_jira_tasks.py --truth validate1/truth.csv --out ~/qwen25-jira-tickets/`.

## Background (not part of the loop)

`docs/ARCHITECTURE.md`, `docs/AUTO-P-ARCH-PROBE.md`, `AUTO-P-GROUND.md`,
`docs/AUTO-H2-CHANGES.md`, `docs/CHANGES-SUMMARY.md`, `docs/COLLECT-24-SUMMARY.md`
— feature design and change logs for the agent itself. `habr-article.md` and the
`habr-test*.md` drafts are the published write-up. `bugs-validate.md`,
`jan-auto-agent-bugs-corrected.md`, `TODO-FIX-pullv3.md`, `FIX-2-ground-file(1).md`
are older hand-written bug reports from before this pipeline existed.
