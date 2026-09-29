# Ground file — multi-model bug-hunt competition

State as of 2026-09-29, branch `kc`. Written so a new session can pick this up
without re-deriving anything. Read this first, then only the files it points at.

The competition has moved on since this file was first written (2026-09-09,
branch `competition`). The five-stage validate/adjudicate/fix harness below is
still the method; the work since then runs as **epic rounds** — one ticket at a
time, every model on the same tree, the winner merged — and the round itself is
now driven by the **Kilo contest** runner. The current state of both is in
[The Kilo contest — where it stands](#the-kilo-contest--where-it-stands) at the
end of this file. Start there if you are picking up rounds.

## What this is

A benchmark harness that measures **whether a model verifies a claim before
acting on it**, using this repo (jan-auto-agent) as the subject. It runs in
these stages:

1. **Find** — jan's own `--auto` mode proposes defects into `IMPROVEMENTS.md`.
2. **Validate** — several reviewer models independently judge every proposal
   against the live code, one entry at a time, writing CSV as they go. Output:
   a report plus `pending-validation.md`, the open questions.
3. **Adjudicate** — several models settle those questions by reading the code.
   Their consensus becomes `truth.csv`, which scores every reviewer and every
   future run.
4. **File** — confirmed defects become tickets in `tasks/`.
5. **Fix** — a coder model works the tickets, one local commit per bug. Either
   driven by hand from `tasks/INDEX.md`, or with `scripts/next_task.py` handing
   out the next unrecorded ticket (resumable — see `docs/FIX-round.md`).

Stages 2, 3 and 5 use the same mechanic: the model never gets the list, only the
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
  **Fixed** — checked 2026-09-29: `SearchAgent().skip_dirs is _DEFAULT_SKIP_DIRS`
  is `False`.
- `tools/auto/arch_probe.py::ArchProbe.last_by_op` — `dict()` is shallow and the
  values are `[hits, misses]` lists, so the tally is mutable from outside.
  **Fixed** — AUTO-P5-v2: it now returns immutable `ProbeOpTally` rows.

Fixed during this work: `StateStore.get_task` / `all_tasks` / `resume_info` all
returned live task dicts, so a caller could mutate one and the next validated
setter persisted it (`status: 12345` reached plan.json). Commit `4796e0a`.

## Verified ground truth

The CSVs are run data and are gitignored; this table is the durable part. It is
the scoring key for every run — recreate `validate<N>/truth.csv` from it, and add
a row each time a finding is settled by hand.

| finding | truth | how it was checked |
|---|---|---|
| `tools/search_agent.py::_DEFAULT_SKIP_DIRS` | **REAL** (fixed since) | identity check: `SearchAgent().skip_dirs is _DEFAULT_SKIP_DIRS` → True; one `append` poisons every later instance |
| `tools/search_agent.py::SearchAgent.__init__` | **REAL** | same defect, reported at the constructor |
| `tools/search_agent.py::SearchAgent.skip_dirs` | **REAL** | same defect, reported at the attribute |
| `tools/auto/arch_probe.py::ArchProbe.last_by_op` | **REAL** (fixed since) | `dict()` is shallow and values are `[hits,misses]` lists from `setdefault(op,[0,0])` — nested mutables shared |
| `tools/metrics_collector.py::MetricsCollector._load_all_cached` | **FALSE** | sole caller `record()` does `records = list(records)  # don't mutate the cached list in place` |
| `tools/auto/state.py::StateStore.get_progress` | **FALSE** | `_progress` only ever holds str/int — `dict()` is a complete copy |
| `tools/auto/controller.py::AutoController.config` | **REAL** (LOW) | форма подтверждена (обычный атрибут, не `@property`); заявленный импакт опровергнут — `task_mode`/`RunLimits` снимаются один раз в `__init__`; но `[gates]` order и `[collect] use_in_auto` читаются живьём, latent |
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
| `docs/TASK-jan-selfaudit.md` | 6 `--auto` goal variants for hunting bugs in jan; variant 1 is a known-answer calibration |
| `VALIDATE-jan-findings.md` | The reviewer prompt — core + 6 variant blocks, CSV schema, how to rank models |
| `docs/JIRA-FIX3-pullv3.md` | The FIX-3 ticket the 11-model round used |
| `scripts/next_finding.py` | Hands out one unrecorded entry at a time |
| `scripts/append_finding.py` | Writes one validated row, immediately |
| `scripts/harvest_report.py` | Stage-2 report: Act/Disputed/Fixed/Dismissed + solo table + scorecard + `--pending` queue |
| `docs/ADJUDICATE-findings.md` | The stage-3 prompt — settling the open questions |
| `scripts/next_pending.py` | Hands out one unsettled question at a time |
| `scripts/append_verdict.py` | Records one REAL/FALSE/FIXED verdict |
| `scripts/truth_consensus.py` | Merges adjudicators into `truth.csv`; escalates splits |
| `scripts/make_jira_tasks.py` | Confirmed defects → `tasks/*.md` + INDEX |
| `scripts/merge_validations.py` | Lower-level "who said what" view |
| `docs/FIX-round.md` | The stage-5 coder prompt — the loop + ground rules |
| `scripts/next_task.py` | Hands out one unfinished `tasks/` ticket at a time |
| `scripts/append_task.py` | Records one ticket outcome (FIXED / ALREADY-OK / SKIPPED) |
| `validate1/` | The 5 reviewer CSVs, `truth.csv`, and the generated report |

## How to run it

```bash
# 1. jan proposes (plan only — no code changes, no commits)
python3 main.py --auto "<goal from docs/TASK-jan-selfaudit.md>" --dry-run \
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
# each adjudicator, given docs/ADJUDICATE-findings.md:
python3 scripts/next_pending.py --pending validate<N>/pending-validation.md \
                                --out validate<N>/truth-<model>.csv
python3 scripts/append_verdict.py --out validate<N>/truth-<model>.csv ...

# then merge (exit 4 if anything is still disputed)
python3 scripts/truth_consensus.py validate<N>/truth-*.csv \
    --truth validate<N>/truth.csv --report validate<N>/adjudication.md
```

Splits are not settled by majority — the code either does the thing or it does
not, and two models agreeing is not evidence when both applied the same wrong
rule, which is how splits usually arise. Read the split yourself, record your
verdict with `append_verdict.py --out truth-<you>.csv`, and re-run with
`--arbiter <you>`: your vote breaks the tie and the others are kept, because a
model that is reliably wrong on split questions is worth knowing about.

The first split was instructive. Three models judged `AutoController.config`:
two said FALSE because no caller mutates it, one said REAL/LOW. The rule says an
unreachable defect is still REAL at LOW severity, so the two were applying the
wrong criterion — and the one that was right had also *disproved the reported
impact* and found a different, genuine one. Agreement with the arbiter on split
questions separates adjudicators far better than overall agreement does.

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

## Known trap: a thin IMPROVEMENTS.md

Round 1 of stage 2 was run against `jan-to-fix/IMPROVEMENTS.md`, in which **all 53
entries carried only a heading** — no Location, no Target files, no Acceptance
check, no Instruction. 5,934 bytes where a full render of the same 53 tasks is
74,332 (`testtext3/IMPROVEMENTS.md`). The renderer in that checkout is byte-identical
to this one and `--validate-plan` had not been run, so the file was truncated
somewhere between generation and use rather than produced that way.

Five reviewers judged 53 proposals from titles alone and produced 261 confident
verdicts. They still found the two real defects — because they read the code
rather than the description — but every number from that round was measured on a
crippled input, and the 88% noise floor in particular is not comparable with a
round run on a full file.

`next_finding.py` now refuses such a file and says so, with `--allow-thin` to
override deliberately. Before trusting a round, check:

```bash
grep -c "^### AUTO-T" IMPROVEMENTS.md      # tasks
grep -c "^\*\*Instruction:" IMPROVEMENTS.md   # should match
```

**A worthwhile experiment:** re-run stage 2 over the full file with the same five
models and compare. Same list, same reviewers, one variable — how much of the
noise floor was jan proposing badly, and how much was the reviewers working
blind?

## Open items

- **The two verified bugs are fixed** (`SearchAgent._DEFAULT_SKIP_DIRS`
  aliasing, `ArchProbe.last_by_op` sharing nested lists). The `truth.csv` rows
  stay `REAL` — that is what they were when judged, and it is what scores a
  reviewer; they are marked "fixed since".
- **Variants 2–6 have never been run.** Only variant 1 (mutable state) has data.
- **`harvest_report.py` promotes a solo NEW to ACT on one vote.** Deliberate,
  but a `--min-votes` flag would let you separate consensus from solo.
- **`truth.csv` has 9 entries.** Every finding you check by hand should be added;
  it raises the resolution of every future run.

## Conventions in this repo

- **Two real test roots, run one after the other, never in parallel and never
  combined:** `python3 -m pytest tests -n 8 -q`, then
  `python3 -m pytest tests_bugfix -n 8 -q`. `.smoke_tests/` and
  `.regression_tests/` are symlink tiers into `tests/` (regenerate with
  `scripts/sync_test_tiers.py`, never by hand) — running them as well runs the
  same files twice. Combining roots in one command still collides conftests.
  As of 2026-09-29 on `kc`: `tests` 6098 passed, `tests_bugfix` 2878 passed,
  about two and a half minutes together.
- `python` is not on PATH; use `python3`.
- Every fix ships a test that fails without it, one commit per concern, commit
  with explicit paths (`git commit -- <paths>`): an operator's `commit -a`
  once swept a round's code into an unrelated commit.
- Verify a reported defect against live code before fixing — this branch's whole
  history says the reports go stale faster than anyone updates them.
- Round data stays out of git: `contest-out/`, `ground/`, `kcNN/`, `fl2/`,
  `*.patch`. Only `contest-bench/<ticket>/` (the judge's suite) is committed.

## The Kilo contest — where it stands

### What it is

`python3 -m tools.contest run --ticket NN` runs one epic ticket as a round:
every model in `contest.ini`'s roster gets its own git worktree
(`../rounds/NN-<agent>`) from the same base commit and works the ticket through
a local `kilo serve` session. A second model is the safety gate for
permissions. At the end of each turn the runner's **harvest** scores the
worktree mechanically — the `PROGRESS.csv` claim, one commit, a test file,
`_shrink` untouched, then the pytest roots on the claimed commit — and says
`READY` or `REWORK`. Everything lands in `contest-out/NN/` (`state.json`,
`SUMMARY.md`, a `<agent>.patch` per entry, `kilo-serve.log`).

The runbook is `docs/kilo-contest/RUN-THE-KILO-CONTEST.md`; the epic is
`docs/kilo-contest/EPIC-KC.md`; the ticket list with what landed where is
`epic-tasks/INDEX.md`.

```bash
python3 -m tools.contest run --ticket NN            # the round (the operator starts it)
python3 -m tools.contest run --ticket NN --dry-run  # plan + first prompt, no server
python3 -m tools.contest status --ticket NN         # the table off state.json, mid-round too
python3 scripts/revive_round.py NN                  # ended agents back in play, then run --resume
```

`revive_round.py` only works on a round that already has `contest-out/NN/`;
a round that was never started is started with `run`, not revived.

### How a round is judged

The harvest's verdict is not the score. A round is scored by a
**judge's acceptance suite** written from the ticket alone —
`contest-bench/<ticket>/acceptance_<ticket>.py` — that drives only the public
contract (the CLI end to end against the fake Kilo in `tests/_kilo_fake.py`,
the documented functions, the files the ticket names). It must be red on the
base. Copy it into every worktree and run it there:

```bash
for d in ../rounds/NN-*; do mkdir -p $d/contest-bench/kcNN
  cp contest-bench/kcNN/acceptance_kcNN.py $d/contest-bench/kcNN/
  (cd $d && python3 -m pytest contest-bench/kcNN/acceptance_kcNN.py -n 8 -q); done
```

Score **every** entry, `STALLED`, `GAVE_UP` and `ERROR` included — their
worktrees hold real work, and the round-83 winner's four equals were all
entries the runner had stopped. Before believing a red, check it is the
entry's fault and not the suite's: in round 83 two entries had taught the
bench's roster the same key the suite wrote, and a strict reading of the
refusal text failed entries the ticket did not. Ties are broken by the
entry's own `tests`/`tests_bugfix`, then by reading the code. The winner lands
on `kc` as-is when it can (author `renat <renat@hp.local2>`), and the
ticket's row in `INDEX.md` and its `**Status:**` line say what landed.

`contest-out/` lives in the checkout that orchestrated the round — `qwen25`
or `qwen26`. Look in both before saying a round never ran.

### Where it stands (2026-09-29)

**The KC epic is done.** KC-44 (round 83) was its last ticket: the ticket's
own `**Size:**` picks the leg count through `[contest] legs_by_size`
(`XS=1, S=1, M=1, L=3`), `--legs` always wins, and an `L` ticket that would
run one leg is refused unless `--legs 1` says so. Winner agnes-3-0-flash,
30/30, landed as-is as `f9bd680`.

Round 83 in numbers: eleven free models, 90 min each. Two `READY` (mimo,
agnes-3), seven `STALLED` (six on the agent's time, glm on 70 min with
no idle), one `GAVE_UP` (step), one `ERROR`
(bynara: the provider dropped the connection). On the suite: five at 30/30,
two at 29, agnes-2 28, mimo and step 27, glm 15 (its `cmd_run` raises).

Fixed on `kc` from what round 83 showed (all unpushed as of this writing):

| commit | what |
|---|---|
| `a325336` | the heartbeat prints one agent per line, name and state padded, so ages and bars line up |
| `5b11111` | the harvest pauses the agent's `agent_max_sec`, as its own suite queue already did (KC-58). Round 83's harvest queue ran 21 and 27 min for two agents, the limit fired inside it, and a `REWORK` ended as `time up` with no rework sent |
| `2cba4a6` | a `PROGRESS.csv` row that claims the round's base is refused. The base is an ancestor of `HEAD`, so it passed as "on the branch": the roots ran on the base, not the change, and `GAVE_UP` named the base as the agent's commit |
| `5d979be` | Kilo's own `session.compacted` in the middle of a turn counts in `compactions` (one per turn). Round 83 had fourteen of them and the table read 0 for all eleven |

**Open, outside KC:** `V4`, `M3`, `V12`, `V13`, `V14`, `V15`, `M6`, `L3` in
`epic-tasks/INDEX.md`.

### What the rounds taught — do not re-derive

- **`state.json` lags.** `status` can show the same numbers for ten minutes
  while every agent is working. The live picture is the runner's own console
  line and `contest-out/NN/kilo-serve.log` (`step=N` per session); a worktree's
  `git status` says whether files move.
- **The heartbeat's `(suite 68s)`** is that agent running a whole pytest root
  in one of the round's suite slots; `(suite queued 3m, 2 ahead)` is it
  waiting for one; `over the ceiling` is past `agent_suite_max_sec`.
- **Two kinds of compaction.** Kilo compacts a session on its own
  (`agent=compaction` in `kilo-serve.log`, `session.compacted` in
  `events.jsonl`); the runner compacts or asks for a summary itself at
  `compact_at_percent`. `compactions` in the table counts both since
  `5d979be`; before that only the runner's and Kilo's overflow path.
- **The harvest queue is the round's, not the agent's.** With many agents and
  few suite slots the judge's own roots can wait half an hour. Since `5b11111`
  that time does not come off the agent's 90 min.
- **A claim can name the wrong commit and still look valid.** Check what the
  roots actually ran on before trusting a `tests_failed`.
- **Free models drop out for provider reasons** (`UnknownError: The model
  service connection was interrupted`, 429s, quota). That is not a verdict on
  the model; the runner keeps the work as a deadline commit.
- **Kilo's `bash` tool defaults to 120 s**; a suite longer than that needs its
  own timeout on the call.
