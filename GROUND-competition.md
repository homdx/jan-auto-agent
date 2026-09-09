# Ground file — multi-model bug-hunt competition

State as of 2026-09-09, branch `competition`. Written so a new session can pick
this up without re-deriving anything. Read this first, then only the files it
points at.

## What this is

A benchmark harness that measures **whether a model verifies a claim before
acting on it**, using this repo (jan-auto-agent) as the subject. It runs in two
stages:

1. **Find** — jan's own `--auto` mode proposes defects into `IMPROVEMENTS.md`.
2. **Validate** — several reviewer models independently judge every proposal
   against the live code, one entry at a time, writing CSV as they go.

The interesting number is not how many bugs get found. It is the **noise floor**
(what share of proposals are wrong) and **who notices**.

## Current results

**FIX-3 round** (11 models, one ticket): every model confirmed a premise I had
written into the ticket as settled fact. The premise was false. Nobody checked
it. Full write-up: `habr-article.md` (published to Habr).

**Validation round** (5 reviewers, 53-entry list, 261 judgements):

| | |
|---|---|
| Noise floor | **88%** — 84 of 96 findings dismissed by everyone who looked |
| Real bugs found | 2, **neither on jan's list** |
| Best reviewer | laguna-s-2-1 (75% accuracy), sensenova-6-8-flash-lite (71%, 2 solo discoveries) |

Both real finds came from `sensenova-6-8-flash-lite` as solo `NEW-*` rows:

- `tools/search_agent.py` — `SearchAgent` binds the module-level
  `_DEFAULT_SKIP_DIRS` by reference; one `append` poisons every later instance.
  **Verified, still unfixed.**
- `tools/auto/arch_probe.py::ArchProbe.last_by_op` — `dict()` is shallow and the
  values are `[hits, misses]` lists, so the tally is mutable from outside.
  **Verified, still unfixed.**

Fixed during this work: `StateStore.get_task` / `all_tasks` / `resume_info` all
returned live task dicts, so a caller could mutate one and the next validated
setter persisted it (`status: 12345` reached plan.json). Commit `4796e0a`.

## The files

| File | What it is |
|---|---|
| `TASK-jan-selfaudit.md` | 6 `--auto` goal variants for hunting bugs in jan; variant 1 is a known-answer calibration |
| `VALIDATE-jan-findings.md` | The reviewer prompt — core + 6 variant blocks, CSV schema, how to rank models |
| `JIRA-FIX3-pullv3.md` | The FIX-3 ticket the 11-model round used |
| `scripts/next_finding.py` | Hands out one unrecorded entry at a time |
| `scripts/append_finding.py` | Writes one validated row, immediately |
| `scripts/harvest_report.py` | The final report: Act/Disputed/Fixed/Dismissed + solo table + scorecard |
| `scripts/merge_validations.py` | Lower-level "who said what" view |
| `validate1/` | The 5 reviewer CSVs, `truth.csv`, and the generated report |

## How to run it

```bash
# 1. jan proposes (plan only — no code changes, no commits)
python3 main.py --auto "<goal from TASK-jan-selfaudit.md>" --dry-run \
    --config agents_128k.ini --base .
python3 main.py --validate-plan --config agents_128k.ini --base .

# 2. each reviewer model, given VALIDATE-jan-findings.md, loops:
python3 scripts/next_finding.py --improvements IMPROVEMENTS.md \
                                --out validation-v1-<model>.csv
python3 scripts/append_finding.py --out validation-v1-<model>.csv ...

# 3. report
python3 scripts/harvest_report.py validate1/*.csv --truth validate1/truth.csv \
    --report harvest.md --actions actions.csv --solo solo.csv
```

## What was learned the hard way

These cost real iterations. Do not re-derive them.

**Instruction does not produce discipline; structure does.** Revision 1 of the
prompt said, in bold, "record each finding the moment you decide it". Every model
still batched and lost its work. The fix was to stop giving them the list:
`next_finding.py` derives the queue from the CSV, so an unrecorded entry is
handed back. Batching became self-defeating instead of merely discouraged.

**Never let a model hand-format CSV.** One model produced 46 malformed rows out
of 46 — 12, 13 and 27 columns where 17 were expected, records fused by unquoted
commas. `append_finding.py` owns the formatting now and there has not been a
malformed row since.

**Require the falsification, not the conclusion.** In run 1 all three reviewers
confirmed `StateStore.get_progress` as a shallow-copy leak. `_progress` only
holds scalars, so `dict()` is a complete copy — they matched the pattern without
checking its precondition. Adding a mandatory `--disproof` field ("what one fact
would make this a non-issue, and what did you find when you checked?") flipped
that verdict to `FALSE_POSITIVE` in run 2 with the reasoning written out.

**Separate reachable from latent.** Reviewers rated dataclass fields `HIGH`
because in Python everything is mutable. `--caller-mutates YES|NO|UNKNOWN` plus a
gate — `HIGH`/`CRITICAL` require `YES` — collapsed severity inflation.

**Skepticism does not rank reviewers.** On a noisy list everyone scores 90%+ by
rejecting nearly everything. Rank on accuracy against a `truth.csv` you verified
yourself, then on solo discoveries that survive checking.

**A solo find is weak evidence about the finding and strong evidence about the
reviewer.** Both real bugs this round were solo `NEW-*` rows from one model.

**One reviewer, one filename.** A model alternated `Kilo.csv` and `kilo.csv`,
halving its own coverage and bypassing the per-file dedupe. Both tools now refuse
a case-variant sibling.

**Stopping early is fine; stopping silently is not.** Best coverage in one
session was 17/53. Because the queue is CSV-derived, re-running the same model
with the same command resumes at entry 18 for free.

## Open items

- **Two verified bugs are unfixed**: `SearchAgent._DEFAULT_SKIP_DIRS` aliasing
  and `ArchProbe.last_by_op` sharing nested lists. Both `LOW`, both real.
- **Variants 2–6 have never been run.** Only variant 1 (mutable state) has data.
- **`harvest_report.py` promotes a solo NEW to ACT on one vote.** Deliberate,
  but a `--min-votes` flag would let you separate consensus from solo.
- **`truth.csv` has 9 entries.** Every finding you check by hand should be added;
  it raises the resolution of every future run.

## Conventions in this repo

- Four pytest roots, run separately — combining them yields ~362 false errors
  from a conftest collision:
  `for d in tests tests_bugfix .smoke_tests .regression_tests; do python3 -m pytest "$d" -q --timeout=180; done`
- `python` is not on PATH; use `python3`.
- Every fix ships a regression test in `tests_bugfix/`, one commit per bug.
- Verify a reported defect against live code before fixing — this branch's whole
  history says the reports go stale faster than anyone updates them.
