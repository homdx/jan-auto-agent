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
   against the live code, one entry at a time, writing CSV as they go. Output:
   a report plus `pending-validation.md`, the open questions.
3. **Adjudicate** — several models settle those questions by reading the code.
   Their consensus becomes `truth.csv`, which scores every reviewer and every
   future run.
4. **File** — confirmed defects become tickets in `tasks/`.

Stages 2 and 3 use the same mechanic: the model never gets the list, only the
next unrecorded item, computed from its own output file. Batching is not
discouraged, it is impossible.

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

## Verified ground truth

The CSVs are run data and are gitignored; this table is the durable part. It is
the scoring key for every run — recreate `validate<N>/truth.csv` from it, and add
a row each time a finding is settled by hand.

| finding | truth | how it was checked |
|---|---|---|
| `tools/search_agent.py::_DEFAULT_SKIP_DIRS` | **REAL** | identity check: `SearchAgent().skip_dirs is _DEFAULT_SKIP_DIRS` → True; one `append` poisons every later instance |
| `tools/search_agent.py::SearchAgent.__init__` | **REAL** | same defect, reported at the constructor |
| `tools/search_agent.py::SearchAgent.skip_dirs` | **REAL** | same defect, reported at the attribute |
| `tools/auto/arch_probe.py::ArchProbe.last_by_op` | **REAL** | `dict()` is shallow and values are `[hits,misses]` lists from `setdefault(op,[0,0])` — nested mutables shared |
| `tools/metrics_collector.py::MetricsCollector._load_all_cached` | **FALSE** | sole caller `record()` does `records = list(records)  # don't mutate the cached list in place` |
| `tools/auto/state.py::StateStore.get_progress` | **FALSE** | `_progress` only ever holds str/int — `dict()` is a complete copy |
| `tools/auto/state.py::StateStore.get_task` | FIXED | `4796e0a` — returns `self._detached(t)` |
| `tools/auto/state.py::StateStore.all_tasks` | FIXED | `4796e0a` — comprehension over `_detached` |
| `tools/auto/state.py::StateStore.resume_info` | FIXED | `4796e0a` — both task lists detached |

To rebuild the CSV:

```bash
awk -F'|' 'NR>2 && NF>3 {gsub(/[` *]/,"",$2); gsub(/[* ]/,"",$3);
  print $2","$3",ground-file,\"see GROUND-competition.md\""}' \
  GROUND-competition.md > validate<N>/truth.csv
# then prepend the header:  finding,truth,checked_by,how
```

## The files

| File | What it is |
|---|---|
| `TASK-jan-selfaudit.md` | 6 `--auto` goal variants for hunting bugs in jan; variant 1 is a known-answer calibration |
| `VALIDATE-jan-findings.md` | The reviewer prompt — core + 6 variant blocks, CSV schema, how to rank models |
| `JIRA-FIX3-pullv3.md` | The FIX-3 ticket the 11-model round used |
| `scripts/next_finding.py` | Hands out one unrecorded entry at a time |
| `scripts/append_finding.py` | Writes one validated row, immediately |
| `scripts/harvest_report.py` | Stage-2 report: Act/Disputed/Fixed/Dismissed + solo table + scorecard + `--pending` queue |
| `ADJUDICATE-findings.md` | The stage-3 prompt — settling the open questions |
| `scripts/next_pending.py` | Hands out one unsettled question at a time |
| `scripts/append_verdict.py` | Records one REAL/FALSE/FIXED verdict |
| `scripts/truth_consensus.py` | Merges adjudicators into `truth.csv`; escalates splits |
| `scripts/make_jira_tasks.py` | Confirmed defects → `tasks/*.md` + INDEX |
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

# 3. report (glob only the reviewer files — truth.csv is not a reviewer)
python3 scripts/harvest_report.py validate<N>/validation-*.csv \
    --truth validate<N>/truth.csv \
    --report validate<N>/harvest.md \
    --pending validate<N>/pending-validation.md \
    --actions validate<N>/actions.csv --solo validate<N>/solo.csv
```

### Running the same scenario more than once

Use a fresh `validate2/`, `validate3/` … per round. Findings are grouped by
`file::symbol`, not by task id, so `AUTO-T1` meaning different things in
different rounds is harmless — and a finding that recurs across rounds with the
same verdict is far stronger evidence than one that appeared once.

`truth.csv` is the one thing that carries forward. Copy it into each new round's
directory; anything already decided is excluded from that round's verification
queue, so you never re-answer the same question.

### The verification queue — the point of `--pending`

`pending-validation.md` lists every finding that **at least one reviewer
confirmed and no human has checked**. At the moment a run finishes, these are not
results — they are open questions. A reviewer may have found a real defect or
pattern-matched a shape that is harmless here, and nothing inside the run can
tell the difference.

The file is written to stand alone: claim, quoted code, reproduction, what the
reviewer checked, and who disagreed — no CSVs needed. Carry it into a separate
session, settle each one against the code, and append the answers to the ground
truth. That is the only step that converts a benchmark result into knowledge.

**Do not treat a solo confirmation as a bug, and do not discard it either.** Both
real defects found so far were solo `NEW-*` rows nobody else raised.

### Stage 3 — settling the queue

```bash
# each adjudicator, given ADJUDICATE-findings.md:
python3 scripts/next_pending.py --pending validate<N>/pending-validation.md \
                                --out validate<N>/truth-<model>.csv
python3 scripts/append_verdict.py --out validate<N>/truth-<model>.csv ...

# then merge (exit 4 if anything is still disputed)
python3 scripts/truth_consensus.py validate<N>/truth-*.csv \
    --truth validate<N>/truth.csv --report validate<N>/adjudication.md
```

Disputes are not settled by majority — the code either does the thing or it does
not, so a split means one side did not run the check it claims. Read those
yourself.

### Stage 4 — tickets

```bash
python3 scripts/make_jira_tasks.py --truth validate<N>/truth.csv \
    --findings 'validate<N>/validation-*.csv' \
    --verdicts 'validate<N>/truth-*.csv' --out tasks/
```

Add a `duplicate_of` column to `truth.csv` for aliases — the same defect often
arrives at the constant, the constructor and the attribute, and that is one
ticket, not three.

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

- **`tasks/` holds 2 generated tickets** for the verified defects; neither is
  fixed yet.
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
