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

### Where it stands (2026-10-01)

Rounds since KC-44 are numbered by ticket (`epic-tasks/NNN-*.md`), judged on
`contest-bench/NNN`, and landed on `kc` (never `main` without an explicit go).

| round | landed on `kc` |
|---|---|
| 126–130 | see `epic-tasks/INDEX.md`; pushed |
| 131, 132 | `9bd4871`, `dafbd20`, `8c58a1d`, ticket+bench `9f99300` (both winners sn68v2) |
| 134 | `f36dbc7`, ticket+bench `73d15eb` |
| 133 | `ec2bc8c` (winner mimo, ran on the second machine), load 3×6347 passed |
| 135 | landed on kc, winner laguna-s-2-1 as-is — see Round 135 below |

Nothing past `ec2bc8c` is pushed. The `kc` → `main` merge message waits for
the operator's go.

**Closing a round (since 135):** one ideal commit (the winner's code keeps the
model as author/trailer `<model> <model@round-NNN.contest>`); every ticket of
the round set to landed/closed **inside that same commit**, with no separate status
commit; then a load test: 3× `tests -n 8` in parallel, then `tests_bugfix -n 8`.

**How a running round is watched — always all three:**
1. the runner's own console tab (one line per agent every minute: state,
   age, bar, files touched);
2. `contest-out/NNN/kilo-serve.log`: `step=N` lines growing means models
   answer. Silent for minutes while every agent is `WAITING 0%` means
   `kilo serve` hung (round 135's first start hung this way and was restarted);
3. `python3 -m tools.contest status --ticket NNN` lags; use it only for the
   end table.
Notes on a round go **here**, in this file, as the round goes, not only in
the session's memory.

#### Round 135 (KC 135, 2026-10-01)

- Ran 14:58–19:09 UTC (15090 s). kilo serve was slow to start (log quiet
  for minutes, every agent WAITING 0 %), not hung — 271 `step=` lines by the
  end. Every `last reason` reads "POST /session timed out": a start-up trace,
  harmless.
- All 11 READY with a commit; glm-4-7-flash finished a round for the first time.
- contest-bench/135: 9 tests, base 3/9, every entry 9/9. Run the bench from
  inside the entry's checkout (copy it in) — run from this checkout it imports
  this checkout's `tools/` and scores the base.
- Code is the same `returncode != 0` raise in all 11. Winner laguna-s-2-1,
  as-is: the only entry whose tests cover all three cases of the ticket
  (failed status, prepare_round leaves no worktree, dirty + clean unchanged).

## Round 136 (AR-1) — run 1, 2026-10-01 23:19

- 11 agents (4× sensenova123, 6× kenary, bynara), base aadbe49. Provider id is `sensenova123`, not `sensenova` (intake refused the latter: no credentials).
- kilo serve (pid 2146779) hung 40 s after the prompts: 100 % CPU, no HTTP answer, log stopped at `snapshot.materialize` 23:20:02. An orphan kilo serve from round 135 (since 14:53, parent systemd, 172 % CPU, ignored SIGTERM) was running beside it; killed with -9, did not revive the server.
- KC-81 detector fired on time (23:30:43, 600 s) with the right advice: kill + restart with --fresh. After kill -9 the runner ended every agent ERROR "event stream closed" by itself; no work lost (0 turns).
- Restarted with --fresh.

### Round 136 (AR-1) — run 2
- 23:32:18 `--fresh`, 11 agents, no orphan this time. All event streams stopped at 23:32:41 right after "project copy refresh done".
- kilo serve pid 2173746: one thread at 100 % CPU, main thread in futex; `~/.local/share/kilo/kilo.db` is 10.5 GB — prime suspect.
- Server gone by 23:41:52; runner ended all 11 agents ERROR "event stream closed", 1 turn, 0 work. Total 9m49s.
- Next: move kilo.db aside (backup), rerun with `--fresh`.

### Round 136 (AR-1) — run 3
- kilo.db (10.5 GB) moved to ~/kilo-db-backup/, fresh db created. Rerun `--fresh` ~23:42: events flow at once — agents edit files, gate permissions pass. Cause of the run 1–2 hangs confirmed: the bloated kilo.db.
- Result (01:11, glm-4-7-flash still running, not waited for): 9 entries scored, mimo-v2-5 ERROR (kenary upstream unavailable / quota).
- Bench: `contest-bench/136/acceptance_136.py` — the second machine's 42-case suite, re-cut to 39: it only knew 3 of the entries' 9 fake-handler seams (now each entry's own test-3 seam), demanded `(AR-\d+)` where the ticket spells `(AR-N)`, and guessed verb names the ticket never gives (only `run start 5` kept).
- Scores: external opus5 39/39 · **sensenova-6-7-var1 38** (echoes `--api_key=` in a usage error) · laguna, sn68-var2, hy3, sn67-var2 37 · sn68-var1, bynara 36 · ext sonet4 35 · agnes 35 (live fake handler: `run start` exits 0) · step 34 (argparse usage block) · ext sonet5 30 · glm 18 (unfinished). Nearly all fail "multiline refuse stays one line".
- Round winner (of ours): sensenova-6-7-flash-lite-var1. Landed code: external opus5 (206 code lines vs 296, 39/39) + sensenova's tests (globals and defaults at the handler, second `--` verbatim, no secret in a usage error, multiline refuse one line). AR-1 landed in the same commit.

## Round 137 (AR-2) — 2026-10-02

- 11 agents (4× sensenova123, 6× kenary, bynara), base 5c67796, `--fresh --variant high --max-parallel 11`. Started cleanly (kilo.db fresh since 136); every checkout had a diff by 06:13.
- First READY: agnes-2-5-flash, hy3, step-3-7-flash, mimo-v2-5. sensenova ×4 show WAITING with 0 turns in `status` while their checkouts already hold code.
- Bench: `contest-bench/137/acceptance_137.py`, 52 cases from the ticket. The repo seam is patched by name (`REPO_ROOT`) or by value, and a separate case checks that it really is the checkout root. View rows are read by position: `emit` masks a column named `key`, so a literal `KEY` header masks every key (Sonet4 shows `***` in the whole first column).
- External patches applied on 5c67796 in ../rounds/137x-{Opus5,Sonet4,Sonet5}.
- Bench grew to 55 with three broken-input cases (an unbalanced quote in `extra`, a broken section header, a key before any section): AR-1's rule is one line, never a traceback. Opus5 tracebacked on all three.
- Scores (glm unfinished, not waited for): **space-bunny-alpha-bynara 55/55** · sn68-var2 53 · ext Opus5 52 · hy3, sn68-var1 50 · mimo 49 · sn67-var1, sn67-var2, ext Sonet4, ext Sonet5 47 · laguna, step 46 · agnes 44.
- Real bugs seen: agnes and step set `REPO_ROOT` one directory off (their tests patch it, so they never noticed); hy3 lets `-p nosuch` pass on `list` and lets a `--base` in `extra` traceback in `view`; Sonet4 names the view column `KEY`, so `emit` masks every key.
- Round winner (of ours): space-bunny-alpha-bynara. Landed code: external opus5 (105 lines of profile.py vs bynara's 206) + the two refusals bynara had (configparser.Error, shlex ValueError) + bynara's tests (base is not a key, broken input, view default/name/-p, refusal inside view, agents_128k never read, REPO_ROOT is this checkout, missing repo). 55/55, AR-1 bench 39/39.
- Ticket 138 = AR-3 (`run start` + `run list`), written on the landed AR-2 API (`cli._load`, `cli.REPO_ROOT`) with this round's lessons in the rules.

## Round 138 (AR-3) — 2026-10-02

- 11 agents, base 9b633e6, `--fresh --variant high --max-parallel 11`, started 08:25; agent_max_sec 5400 stopped 9 of them STALLED with a deadline commit at ~09:55; READY only mimo-v2-5 and step-3-7-flash.
- New hazard: sensenova-6-8-var2 and sensenova-6-7-var1 tried their own `arena run start 5` for real — it built `arena-round/5` in their checkouts and started a detached `tools.contest run --ticket 5` with its own `kilo serve`. Killed by hand. Next tickets: verify only with `--dry-run`; a real `tools.contest run` / `arena run start` should go to the gate (ask_commands).
- Bench: `contest-bench/138/acceptance_138.py`, 44 cases from the ticket; the spawn seam is patched by name (`SPAWN`) or by value (`subprocess.Popen` alias); step 0 proven with a real `run --dry-run` on the KC-16 sandbox, ticket only on `arena-round/2`.
- Scores: ext cloud Opus 5 (branch ar-3-arena-run-start-list) 44/44 · ext Opus5 patch 44 · **space-bunny-alpha-bynara 43** · ext Sonet5 43 · mimo, sn67-var2 41 · sn68-var2 40 · hy3 38 · laguna 37 · sn68-var1 26 · sn67-var1 24 · step 15 · agnes, glm 5 (unfinished).
- Common miss: `ticket_file` drops the ticket's trailing newline (`git show` output stripped) — bynara, Sonet5, mimo, sn67-var2.
- Landed: cloud Opus 5 as-is (588 code lines vs the patch Opus5's 675, 27 own tests vs 17). It edits one line of test_arena_cli.py (the "not implemented" example moves from `run start` to `run view`): unavoidable, `run start` is now implemented.
