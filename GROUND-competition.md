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

## Round 140 — AR-59 `arena model available|use|drop` (2026-10-02)

- Bench `contest-bench/140/acceptance_140.py`: 19 cases, fake `kilo` on PATH, no ini; flags accepted after the verb or as arena globals.
- 19/19: Sonet5-4, sn68-var1, sn67-var2, sn68-var2 (fails on the real `kilo_bin = auto`). Cloud Opus 18/19 (two stderr lines on a kilo failure), mimo 18, Sonet4 18, Grok4-7 17, step 16, GPT6-Luna 15, agnes-2-5 11, bynara 8. laguna, hy3, longcat DEAD (kenary free rate limit, bynara socket drops).
- Winner Sonet5-4: smallest full pass (608+68), 44 tests, works on real kilo 7.6.2. Ideal b63ea2f on top of the cloud 02d6328 (the branches had diverged; local AR-4 commit rebased → d77cc32). Ticket 141 open 385d2d6.
- Tests 6484/2878; stress 3× clean.

## Round 141 — AR-60 direct providers, `arena model test`, scores (2026-10-02)

- 11 agents, base 385d2d6, `--fresh --variant high --max-parallel 11`; READY sn68-var1, sn67-var2; STALLED with a deadline commit agnes, mimo, step, bynara, sn68-var2, sn67-var1 (all judged); DEAD laguna, hy3, longcat (no edit in two first-touch rounds).
- Gate: 167 permission asks, 2 to the LLM (agnes `> /tmp/x` rejected, correct; bynara heredoc allowed), 1 mechanical reject (sn67-var1 `cat ~/.local/share/kilo/tool-output/…` — Kilo's own truncation file of the agent's own output, a near false positive).
- Bench `contest-bench/141/acceptance_141.py`: 17 cases one layer under the entries' seams — a fake `kilo` answers `models` and `run`, the direct provider is a real HTTP server on port 0, py_model_test's 15 checks run for real. AR-59 bench 140 run alongside.
- Scores 141 + 140: cloud Opus 5.5 17+16 (built on the old cloud AR-59, breaks it) · mimo 16+17 · **cloud Sonnet 5 15+19** · sn67-var2 15+19 · bynara 10 · sn68-var1 9 · agnes, sn67-var1, sn68-var2, cloud Sonnet 4 8 · step 5. Common miss: `kilo models NAME` failing for a provider not in Kilo became a refusal instead of the no-key / no-URL hint.
- Landed e885ba1: cloud Sonnet 5 + judge fix (unknown provider = hint, also after a failed direct list; Kilo broken = refusal) + hints name the provider as typed. Tests 6503/2878, stress 3× clean.
- 12th entry (external patch comit-ticket.patch, also Sonnet 5): 15/17 + 19/19, the same miss; its 39 tests run against the ideal found one more hole — a failed direct list fell back to Kilo and refused in two lines for a provider Kilo lacks; fixed in the landed commit.

## Round 139 — AR-4 `arena run view NN[.K]` (2026-10-02)

- First round started through arena: `./arena -p p139 run start 139 -- --max-parallel 8`, base b3119fe, 8 agents; the runner got `--max-parallel 8` twice (profile `extra` + passthrough) → AR-62. Parallelism had to go into `contest.local.ini` by hand → AR-61. Both are ticket 142.
- READY agnes, mimo, sn67-var1/2, sn68-var2, step; ERROR bynara (model connection dropped, deadline commit), sn68-var1 (stopped by the operator, committed). Plus 3 cloud entries: Sonnet 5, Opus 5.5, Sonet.
- Bench `contest-bench/139/acceptance_139.py`: 25 cases, fake `/proc` through `rounds.PROC_ROOT`, `state.json` in a real round's shape. One case was too strict: without `base_sha`, `cmd_status` itself exits 1, and the ticket passes its code through, so only the `base ?` header is checked.
- Scores: 25/25 cloud Sonnet 5, cloud Opus, agnes, mimo, sn67-var1, sn67-var2, sn68-var1, sn68-var2; 23/25 step, bynara, cloud Sonet (JSON `agent` = the spec dict, not the name). AR-3 bench 138 44/44 for all.
- Rival tests against each other: sn68-var2's tests found `leg 2/1` (N counted folders, not the last leg number) in every other entry.
- Landed faf9dd5: cloud Sonnet 5 (smallest code of the eight) + leg N fix + test. Tests 6524/2878, stress 3× clean.

## Round 142 — AR-61 `arena profile set` + AR-62 a flag once (2026-10-02)

- Started `./arena -p p139 run start 142`, base c6619d2, 8 agents: READY sn68-var1/2, sn67-var1/2, agnes, mimo, step; bynara STALLED (90-minute deadline commit, worked to the end — `WAITING` with 0 tokens in the table only means the runner records counters at the end of a turn). Plus 2 cloud entries: opus5-142, sonnet5-142-arena-profile-set.
- Bench `contest-bench/142/acceptance_142.py`: 43 cases through `cli.main` and `rounds.build_run_line`. 43/43 for 8 of 10 — too soft, so edge probes on top (a flag twice in the passthrough, `profile set` without NAME, a second writer).
- Out: agnes and step 42/43 (`fresh = maybe` in the ini not refused on read); step also turns `--variant a --variant b` into `a --variant b`; agnes takes `profile set max_parallel=2` as profile `max_parallel=2`; bynara has its own ini writer in profile.py beside models.py (the ticket said one writer).
- Clean: mimo, sn67-var1/2, sn68-var1/2, cloud Opus 5, cloud Sonnet 5 (Sonnet also flipped the ticket status itself).
- Landed 25d3cfa: cloud Opus 5 as-is — smallest diff (169 code lines), `_with_models` → `_with_key` + `write_profile_keys`, `dedupe_flags` in profile.py used by `build_run_line`. Tests 6547/2878, benches 138/140/141 green, stress 3× clean.

## Round 143 — AR-5 `arena run rerun NN[.K]` (2026-10-02)

- Started `./arena profile set p139 max_parallel=8 fresh=yes -y` + `./arena -p p139 run start 143`, base 0285c27, 8 agents — the first round with parallelism from `profile set` (the runner got `--max-parallel 8` once). READY sn68-var1/2, sn67-var1/2, agnes, mimo, step; bynara STALLED (90-minute deadline commit). Plus 3 cloud branches: opus5-143, sonet5-143, sonet4-143 (sonet4 sits on the AR-4 base b3119fe and implements AR-4 again — not an entry).
- Bench `contest-bench/143/acceptance_143.py`: 30 cases, a git repo under tmp_path, stub `SPAWN`, fake `/proc`, plus `revive_round.py` output pinned byte-for-byte. First draft too soft on the exit-2 mapping: it back-dated state.json, hiding that a rerun's own revive write already counts as "rewritten after the start" under `run start`'s `time.time() - 1`. Second trap was the stub itself: a rewrite in the same coarse kernel tick as arena's write keeps the same mtime, so the stub stamps its rewrite +5 s.
- Scores (3 passes each): 30/30 cloud Sonnet 5, sn68-var1, bynara (WIP, a 5-second mtime wait loop); 29 cloud Opus 5, mimo (exit 2 → 4 even when the runner never touched state.json), sn67-var2, sn68-var2 (exit 2 rewritten → 1); 28 agnes (`--legs` twice, no -y refusal for an earlier leg), sn67-var1 (writes state.json before the no-base refusal); 26 step. Bench 139's `run rerun` "not implemented" case moves to `issue list`.
- Landed 97a5ed2: cloud Sonnet 5 as-is — shared `_run_child` for start and rerun (sn68-var1 equal on score but duplicates the lock + wait), `started = nextafter(own write mtime)`. Ticket closed by the entry itself.

## Round 144 — AR-6 `arena issue list` / `issue view NN` (2026-10-03)

- Started `./arena -p p144 run start 144`, base 4541ea1, 12 agents, `max_parallel=12`, `agent_max_sec = turn_max_sec = 14400` in contest.local.ini. READY sn68-var1/2, sn67-var1/2, mimo, step, bynara, gpt-5.4-mini, glm-4.5 (uncommitted files); GAVE_UP agnes (tests_failed); STALLED glm-4.7 (80 min, no idle); ERROR deepseek-v4-flash-free (free tier refuses a prompt that long — drop it from free rounds). Plus cloud branches opus5-144, sonet5-144; sonet4-144 implements AR-4 again — not an entry.
- Bench `contest-bench/144/acceptance_144.py`: 26 cases, a git repo on branch `arena`, one ticket per state, fake live runner, flags, filter, json, read-only. Base 7/26. Scores: 26/26 for ten entries (all READY ones but glm-4.5, plus cloud Opus 5 and Sonnet 5); agnes 24, glm-4.5 5, glm-4.7 / deepseek / sonet4 7 (base).
- Bench too soft again, so edge probes + every entry's tests against every implementation: the checkout and the branch usually hold the same two-files pair, and gpt-5.4-mini, sn67-var1/2, bynara, step print that flag twice; every local entry prints `issue view -o json` as a one-element list, not the object the ticket asks for. Only cloud Opus 5 and Sonnet 5 clean on both.
- Landed 4b74e98: cloud Sonnet 5 as-is — like Opus 5, plus an unreadable ticket file is a refusal (OSError) and flag lines go through `scrub`; it also moved bench 139's not-implemented case to `issue create` and set the ticket `landed` itself. Tests 6578 + 2878, benches 139/142/143/144 green, stress 3× clean except `test_committed_turn_extension_limits_are_a_floor_below_a_ceiling`, which reads the local ini's 14400 (fails on the base too).

## Round 145 — AR-7 `arena issue create` (2026-10-03)

- Ran on arena before the overflow fixes (base 646c3eb, 11 agents): READY sn68-var1/2, sn67-var1/2, agnes, mimo; GAVE_UP step (tests_failed); STALLED bynara (130 min, uncommitted); ERROR gpt-5.4-mini (wallet), glm-4.5 ("Prompt exceeds max length" — the overflow 147–149 now read), glm-4.7 (overloaded).
- Judged after merging ctx-overflow-fix into arena (58fe231). Bench `contest-bench/145/acceptance_145.py`: 53 cases from the ticket (numbering sources, --number, brief layout and section cut, every refusal before any model call, lint/review rejection holds the number, real draft_callables refusals, json, out_dir, key never printed). 53/53: sn67-var1/2, sn68-var1/2, bynara; mimo 52, agnes 51, step 50, glm-4.5 30, glm-4.7 5, gpt 4.
- Tie-breaks: full tests (bynara 19 failed, 21 min), every entry's own tests crossed over every implementation — sn68-var2 passes mimo's whole file and fails others only on refusal wording and `reviewed` on a lint refusal (the ticket ties it to --no-review only).
- Landed 68c0466: sn68-var2 as-is. It also moved the "not implemented" example to `issue land` in bench 143/144 and test_arena_issue_list — the same edit the ticket sanctions for 139. Clean-checkout tests 6794 + 2882 green, benches 139–149 green, load 3×3.

## Round 151 — the event-tap reconnect and the `.kilo` project-file guards (2026-10-04)

- Ran on arena (base c209c57, 17 agents, 90 min): READY sn68-var1 (off-ticket `.kilo-scratch*` files), sn67-var1/2, agnes; ERROR with work left uncommitted — sn68-var2, bynara, glm-4.7-flash (prompt_async timeout); ERROR mimo, step (provider unavailable), apertus-8b/-v1.5-8b-thinking (quota); GAVE_UP the other apertus and both SEA-LION. Plus three cloud Sonnet 5 patches (5a/5b/5c), judged the same way.
- Bench `contest-bench/151/acceptance_151.py`: 28 cases through `KiloBackend` over the fake, `_run_one` and the `--resume` sandbox, no private helper named. Base 10. Final scores: arena's landed code 28; Sonnet 5c 27; Sonnet 5a/5b 26 (B10w, A4 — 5b's one deadline is 15 s, the old worst case); bynara 26 (2 own overflow tests broken); sn68-var2 as it left its worktree 25; sn67-var1/2 24; agnes 22; sn68-var1 22; glm 13; apertus-70b-thinking does not import.
- Bench traps: the first `KiloServer.spawn` in `run` is intake's throw-away offer check (`kilo-offer-*` log), not the round's server; B10 first demanded more than the ticket (an agent's rule file visible) — cut back to "kilo.jsonc never in `git status`", plus B10w for a linked worktree, where the rounds run.
- Full tests: all three Sonnet clean (6806–6830 + 2883); agnes, sn67-var1/2, sn68-var1/2 clean except `test_symlink_out_of_the_worktree_is_judged_by_its_target`, which fails only for a long `--basetemp` (the reason string is cut). bynara breaks 2 overflow tests; glm 16 + tiers; apertus 20 + 30 errors.
- Live, Kilo 7.6.2, no model call: `KILO_CONFIG_CONTENT` outranks `.kilo/kilo.jsonc` field by field (ticket 153, needs a `:free` live check); the project file is read on first open; Kilo writes its own `.kilo/.gitignore` (no project names) into a `.kilo/` without one and never rewrites it. Cloud Sonnet 5a saw the file written without `$schema`; here it had one.
- **Cross-tests (now mandatory at every acceptance):** every entry's own test file run on every implementation, 10 × 10. Most red cells are `api` — another exception name (`KiloTapLost`, `KiloReconnectFailed`), a private helper, a mock of `_git` with another signature, a log wording. Run with the exception name mapped to ours, the behaviour cells on arena's code found two real bugs in the winner: `interrupt()` held behind the whole reconnect (159) and two taps on one `events.jsonl` (160); fixing them, the winner's own tests caught that `EventTap.wait` consumes the one `tap.closed` (re-armed in both fixes). Idea filed as ticket 158: the judge does the matrix itself.
- Landed: 808cdf8 sn68-var2 as-is (best local entry, and its tests are the most complete), then one ticket and one commit per bug: 154 (Kilo's own `.gitignore` left `kilo.jsonc` visible), 155 (the ignore trusted from a cache; the agent's `git clean` or the drop removed it behind it), 156 (ignore written into a non-checkout — bench 148 b3), 160, 159. Queued: 153, 157 (arena run lock written after the child starts — a flake, cause in the code), 158.
- Disk: eight entries' suites and load passes with private basetemps filled every inode of `/` — delete each basetemp after its run.
