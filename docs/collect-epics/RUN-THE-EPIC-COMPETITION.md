# Running the epic round — several agents, one ticket at a time

The validation round put N models on the same **findings**. This puts N agents
on the same **ticket**: everyone implements round 1 against the same tree, you
merge the one that did it best, and round 2 starts from the merged result.

It reuses the fix-round machinery unchanged — `scripts/next_task.py`,
`scripts/append_task.py`, one progress file per agent. The only new pieces are
`scripts/split_epic_tickets.py` (which turned the epics into that machinery's
ticket shape) and `scripts/judge_epic_round.py` (which fills in the mechanical
half of the scorecard).

All paths are from the repo root. `python` is not on PATH — use `python3`.

---

## The map

| # | stage | who | command | you take away |
|---|---|---|---|---|
| 0 | **split** | you, once | `split_epic_tickets.py` | `epic-tasks/NN-*.md` + `INDEX.md` |
| 1 | **hand out** | you, per round | one worktree per agent, same base commit | N branches |
| 2 | **implement** | each agent | `next_task.py` → commit → `append_task.py` | one commit each |
| 3 | **score** | you | `judge_epic_round.py` | the mechanical table |
| 4 | **judge** | you | read the diffs | the winner |
| 5 | **merge** | you | cherry-pick the winner onto `competition` | the base for round N+1 |

Rounds are **serial on purpose.** Round 5 (`V2`) rewrites the function round 6
(`V3`) adds rows to. Running them in parallel produces N conflicting versions of
the same file and nothing to merge.

---

## Stage 0 — split the epics into tickets (once)

```bash
python3 scripts/split_epic_tickets.py
```

24 tickets into `epic-tasks/`, in round order, each one self-contained: the
ticket body from the epic, plus the ground rules restated inline. `INDEX.md` is
the table.

The short path — 11 rounds instead of 24, ~80% of the value:

```bash
python3 scripts/split_epic_tickets.py --order short --out epic-tasks-short
```

The order lives in `ROUND_ORDER` at the top of the script. Editing that list is
how you re-plan; re-running the script rewrites the folder.

**Commit `epic-tasks/` before stage 1.** `git worktree add` checks out the
branch, so an untracked folder does not exist inside the agents' worktrees and
`next_task.py` finds nothing:

```bash
git add epic-tasks/ docs/collect-epics/ scripts/split_epic_tickets.py scripts/judge_epic_round.py
git commit -m "epic round: split the collect epics into 24 ordered tickets"
```

`runs/` is already gitignored and stays untracked — that is deliberate, so the
per-agent progress files never appear in the diffs you are comparing.

---

## Stage 1 — hand out one round

**One worktree per agent, all from the same commit.** Agents committing into one
checkout collide; that is not a tool problem, it is what worktrees are for.

```bash
BASE=$(git rev-parse --abbrev-ref HEAD)          # e.g. competition
for a in opus sonnet qwen glm; do
  git worktree add "../round-$a" -b "epic-$a-r01" "$BASE"
  mkdir -p "runs/$a"
done
```

`runs/` is gitignored, so the progress files stay out of the commits you are
comparing.

**Give each agent:**

| give it | why |
|---|---|
| its own worktree, on its own branch | it commits |
| the prompt below, verbatim | the loop + the ground rules |
| `epic-tasks/` | the tickets — `next_task.py` hands them out one at a time |
| `docs/collect-epics/PLAN-v2.md`, `EPIC-M-metrics.md`, `EPIC-L-live-findings.md` | context for *why*, when the ticket alone is not enough |

`MEASURE-BEFORE-AFTER.md` is for **you**, not the agent — it is the protocol
for the numbers a round is judged against, and an agent that reads it tends to
start optimising the metric instead of implementing the ticket.

Do **not** give it `docs/collect-epics/EPIC-A/B/C` unless you are deliberately
running v1 — they are the superseded plan and several of their numbers are
wrong (see `INDEX.md` §"Numbers in this file … are hand-measured").

### Prompt — paste this verbatim

**▼▼▼ PROMPT STARTS ▼▼▼**

> You are implementing one ticket from an epic round. Every agent in this round
> is implementing the same ticket against the same starting tree; the best
> implementation is merged and becomes the base for the next round. You are
> being scored on the implementation, not on speed.
>
> Get your ticket:
>
> ```bash
> python3 scripts/next_task.py --tasks epic-tasks/ --progress runs/<YOUR NAME>/PROGRESS.csv
> ```
>
> Implement **exactly that ticket**. Then record it:
>
> ```bash
> python3 scripts/append_task.py --progress runs/<YOUR NAME>/PROGRESS.csv \
>     --ticket <the NN-*.md you were given> --outcome DONE --commit <sha> \
>     --note "one line: what changed + the test that covers it"
> ```
>
> Stop after **one** ticket. Do not call `next_task.py` again — the next round
> is handed out separately, from a different tree.
>
> The ground rules are printed inside the ticket. Three of them settle the round
> on their own, so read them before you start:
>
> - `CollectBridge._shrink` must be **byte-identical** when you are done. New
>   work runs *before* it, never instead of it. This is checked mechanically.
> - **One local commit.** Never `git push`.
> - **A test ships with the change**, and it must fail without the change.
>
> Two more that are checked by reading your diff:
>
> - Everything you add is **fail-open**: an absent collect model, a malformed
>   config key or a broken artifact degrades to "no collect data" and never
>   raises into a run.
> - **Stay on the ticket.** Touching files the ticket does not name counts
>   against you unless you say why in the commit message.
>
> Never point any command at a live provider config. If a step needs one, copy
> `agents_128k.ini` to a scratch path and stub every `base_url` first.
>
> When you are done, report: the commit sha, each Acceptance checkbox and
> whether you met it, and anything in the ticket you found to be wrong about the
> live code — the tickets were written against commit `68b78a0` and the code is
> the authority, not the ticket.

**▲▲▲ PROMPT ENDS ▲▲▲**

Replace `<YOUR NAME>` before sending. Name the run folder after the agent,
lowercase — the same convention the reviewer CSVs used.

---

## Stage 3 — the mechanical table

```bash
python3 scripts/judge_epic_round.py --round 1 --base competition \
    --worktree opus=../round-opus --worktree sonnet=../round-sonnet \
    --csv runs/round01-scorecard.csv
```

or, if the worktrees are all under one folder, `--runs ../rounds/`.
Add `--tests` to run the four pytest roots in every worktree — slow, and worth
it on any round that touches `tools/`.

Three columns are **hard gates**; a `FAIL` settles that agent's round:

| gate | why it is absolute |
|---|---|
| `shrink` ≠ `same` | the one constraint every ticket carries. `_shrink` was written and debugged over a long stretch; a round that touches it is not a round we can merge. |
| `commits` ≠ 1 | you cannot cherry-pick a winner cleanly out of five commits, and it hides scope creep. |
| `pushed` = `yes` | nothing in this round goes to a remote. |
| `test_files` = 0 | an implementation with no test is not comparable to one with a test. |

The rest are columns, not verdicts. `off_ticket` is the one to look at
second — a diff that touched four files the ticket never named usually did
something else as well.

---

## Stage 4 — the judgement the script cannot make

Read the winning candidates' diffs against these four, in order. They are the
scorecard.

1. **Acceptance, item by item.** Every ticket ends in a checkbox list written
   before any agent saw it. Walk it. An implementation that meets six of eight
   loses to one that meets eight, whatever else it did.
2. **Does the test fail without the change?** Revert the source hunk, keep the
   test, run it. A test that passes both ways is not a test. This catches more
   bad rounds than anything else on this list.
3. **Is it the simplest thing that works?** The whole reason `PLAN-v2` exists is
   that v1 built a `FactRow` dataclass, an `assemble()` method, a configurable
   priority ladder and seven config keys for what is ten lines of fixed order.
   A new module, a new abstraction or a new config key that the ticket did not
   ask for is a mark **against**, not for.
4. **Fail-open, still.** Feed it an absent model, a malformed key, a truncated
   artifact. It must degrade to "no collect data" and never raise. Every ticket
   inherits this; almost none of them restate it in the Acceptance list.

Then the tie-breaker: **which diff would you rather maintain in six months.**

Record the decision — one line per round, in `runs/DECISIONS.md`:

```
round 01  L2   winner: opus     why: only one that kept `usable` unchanged and tested all three status values
round 02  M1   winner: sonnet   why: numbers matched qwen's exactly; script was 40 lines shorter
```

That file is the thing worth keeping. It is also the honest record of which
agent is actually better, accumulated over 24 rounds instead of guessed.

---

## Stage 5 — merge and start the next round

```bash
git checkout competition
git cherry-pick <winning sha>
python3 -m pytest tests -q --timeout=180 && python3 -m pytest tests_bugfix -q --timeout=180
git worktree remove ../round-opus   # …and the rest
```

Then stage 1 again, from the new `competition` head, with round 2's ticket.

**Do not carry a worktree across rounds.** A fresh worktree from the merged
head is what makes round N+1 comparable; a stale one is an agent building on
its own losing round.

---

## The two rounds that are scored differently

**Measurement tickets (`M1`, `M2`, `M3`, `M6`) produce numbers, not features.**
The scorecard changes: the winner is not the nicest script, it is the one whose
**numbers other agents' scripts independently reproduce**. Run every agent's
script, diff the outputs.

- Numbers agree across agents → merge the shortest script, and the agreement is
  itself the evidence the baseline is real.
- Numbers disagree → **that is the finding.** Do not merge either until you know
  which is right. The whole reason `M1` exists is that v1's hand-measured
  numbers said 483 modules where the artifact has 469.

**`M3` is a go/no-go, and it gates rounds 17 and 18.** It measures the ceiling
on the static suppressor — how many of `validate1/truth.csv`'s 54 adjudicated
findings resolve to one of the ~144 locations the artifact can call safe. Read
its decision table before handing out `V12`:

| M3 result | what happens to rounds 17–18 |
|---|---|
| 0 findings resolve | **skip both.** `V11` is the whole of the bug-hunting work. |
| 1–5 | run `V12` as the ~40-line version in the ticket, line-citations only. Skip `V13`. |
| >5 | run both as written. |

Deciding this *after* the measurement instead of before it is the point.

---

## Standing constraints for the whole round

They are in every ticket, and they are repeated here because they are the ones
that end a round rather than cost it points.

1. **`CollectBridge._shrink` stays byte-identical.** Checked mechanically by
   `judge_epic_round.py`. New work runs before it, never instead of it.
2. **Nothing runs against a live provider config.** Copy `agents_128k.ini` to a
   scratch path and stub every `base_url`. `M5`'s A/B harness carries this in
   its own Acceptance list; it applies to every round.
3. **Never `git push`.** Local commits only, for every agent, every round.
4. **Do not overwrite a `.collect/` artifact without backing it up.** Several
   tickets rebuild one; the trees under `../testtext*` are live competition data.
5. **Four separate pytest invocations.** One combined call produces ~362 false
   errors from a conftest collision.

---

## Where the tickets came from

| source | tickets | what it is |
|---|---|---|
| [`PLAN-v2.md`](PLAN-v2.md) | `V1`–`V15` | the recommended plan: 15 tickets, no new modules, two config keys |
| [`EPIC-M-metrics.md`](EPIC-M-metrics.md) | `M1`–`M6` | measure before, measure after — the only way to compare v1 and v2 |
| [`EPIC-L-live-findings.md`](EPIC-L-live-findings.md) | `L1`–`L3` | from reading five live `--auto` runs; see [`LIVE-RUN-VALIDATION.md`](LIVE-RUN-VALIDATION.md) |
| `EPIC-A/B/C` | — | **plan v1, superseded.** Kept for diffing against v2, not for implementing. |
