# Fixing the tickets — the stage-5 loop

Stage 4 (`scripts/make_jira_tasks.py`) turned the adjudicated ground truth into
one ticket per confirmed defect under `tasks/`. This is how a coder model works
through them.

> This file lives in `docs/`, but **every path below is written from the repo
> root** — that is where the coder runs, and where all the commands assume it is.
>
> This is stage 5 of five. For the whole round — what each stage hands the model
> and what you take away from it — see [`docs/RUN-THE-COMPETITION.md`](RUN-THE-COMPETITION.md).

There are **two ways to run it**. Both use the same `tasks/*.md` tickets and the
same ground rules; they differ only in how the coder is handed the next one.

---

## What to hand the coder

Everything a coder needs is in the repo. There is no separate fix-round briefing
document to find — [the ground rules](#ground-rules-for-the-run--give-these-to-every-model-verbatim)
below are it, and each ticket restates them in its own `## Acceptance` section.

**One agent, working alone:**

| give it | why |
|---|---|
| the repo, checked out on a branch of its own | it commits |
| `docs/FIX-round.md` (this file) | the loop + the ground rules — paste it, or point at the path |
| `tasks/` | the tickets. Variant 2 hands them over one at a time; you do not need to paste them |
| `scripts/next_task.py`, `scripts/append_task.py` | already in the repo — the coder just runs them |

Nothing from `validate1/` is needed **during** the round. Each ticket already
carries the defect, the evidence, the reproduction and the disproof inline —
that is what stage 4 built them for, and a coder that goes reading the reviewer
CSVs is reading opinions instead of code. `validate1/truth.csv` matters only
[after the round](#after-the-round), when you record what was fixed.

**The prompt to paste is not the same for the two variants.** Pick a variant
first, then copy the exact text from its own **"Prompt — paste this verbatim"**
box below:

- Variant 1 (you feed the tickets by hand) → [its prompt box](#prompt--paste-this-verbatim)
- Variant 2 (a script feeds them, resumable — preferred) → [its prompt box](#prompt--paste-this-verbatim-1)

In both cases the ground rules further down are part of the prompt — paste this
whole file, or paste the box plus the ground-rules section.

**Several agents at once:** same list, plus one `--progress` file and one
worktree each — see [Running several models over the same tickets](#running-several-models-over-the-same-tickets-at-once).

---

## Variant 1 — you drive the loop, by hand

You hand the model `tasks/INDEX.md` (the table of all tickets, always in sync
with `tasks/`) plus the ground rules, and it walks the list top to bottom, one
commit per ticket.

This is the older, unscripted way and it still works. What it does not give you
is the resume: the model holds the whole list in context, so an interrupted run
loses whatever was not committed, and you cannot tell from disk how far it got.
Prefer variant 2 unless you have a reason not to.

### Prompt — paste this verbatim

> You are fixing confirmed defects in this repository. The list is in
> `tasks/INDEX.md`; each row links to a ticket file under `tasks/` with the
> defect, its evidence, a reproduction and the disproof to check first.
>
> Work the tickets **in the order they appear in `tasks/INDEX.md`**. For each
> one: read the linked `tasks/NN-*.md`, verify the defect against the live
> source, then either fix it with **one local commit** (never `git push`) or, if
> it is not real, note that and move on. A regression test in `tests_bugfix/`
> ships with every fix. Do the next ticket only after the current one is
> committed. When every row is done, stop and report, per ticket, what you did
> (fixed + commit sha / already-ok / skipped + reason).
>
> Follow the ground rules in `docs/FIX-round.md` §"Ground rules for the run"
> exactly — they are how the work is scored.

---

## Variant 2 — a script hands out the next ticket (resumable, preferred)

Same mechanic as `next_finding.py` in the validation round. The model never gets
the whole folder, only the next ticket whose outcome is not yet recorded. If the
context fills up mid-round, **restart the model with the same command** — it
resumes at the first unrecorded ticket, because the queue is derived from
`tasks/PROGRESS.csv` on disk, not from anything the model is holding.

### The loop

```bash
# 1. ask for the next ticket
python3 scripts/next_task.py --tasks tasks/

# 2. fix exactly that ticket (see ground rules below), then ONE local commit

# 3. record the outcome — this is what advances the queue
python3 scripts/append_task.py --progress tasks/PROGRESS.csv \
    --ticket <the NN-*.md you were given> --outcome FIXED --commit <sha> \
    --note "one line: what changed + the regression test"
```

Then back to step 1. Running step 1 twice without recording hands you the **same**
ticket again — you cannot skip ahead, and nothing you did counts until it is in
`PROGRESS.csv`.

`--outcome` is one of:

| value | meaning | requires |
|---|---|---|
| `FIXED` | code changed, one local commit, regression test in `tests_bugfix/` | `--commit` |
| `ALREADY-OK` | verified against the live code, no defect / already handled | — |
| `SKIPPED` | deliberately deferred (out of scope, latent-only, blocked) | `--note` |

`python3 scripts/next_task.py --status` prints progress without handing anything out.
Exit code 3 from either command means the folder is finished.

### Prompt — paste this verbatim

> You are fixing confirmed defects in this repository, one ticket at a time. Do
> **not** open `tasks/` yourself — a script hands you the next ticket:
>
> ```bash
> python3 scripts/next_task.py --tasks tasks/
> ```
>
> It prints one ticket (defect, evidence, reproduction, disproof) and the exact
> command to record it. Verify the defect against the live source, then either
> fix it with **one local commit** (never `git push`) or decide it is not real.
> Record the outcome:
>
> ```bash
> python3 scripts/append_task.py --progress tasks/PROGRESS.csv \
>     --ticket <the NN-*.md you were given> --outcome FIXED|ALREADY-OK|SKIPPED \
>     --commit <sha> --note "one line: what changed + the regression test"
> ```
>
> Then run `next_task.py` again. It re-hands the same ticket until you record it,
> so you cannot skip ahead, and if this session is cut off just re-run the same
> command — it resumes at the first unrecorded ticket. A regression test in
> `tests_bugfix/` ships with every fix. Stop when `next_task.py` exits with code
> 3 ("every ticket is recorded") and report your outcome counts.
>
> Follow the ground rules in `docs/FIX-round.md` §"Ground rules for the run"
> exactly — they are how the work is scored.

### Running several models over the same tickets at once

The queue is derived from the progress file, so **the progress file is what
separates one agent from another**. Point each model at its own:

```bash
python3 scripts/next_task.py   --tasks tasks/ --progress runs/<model>/PROGRESS.csv
python3 scripts/append_task.py --progress runs/<model>/PROGRESS.csv --ticket ... --outcome ...
```

Every model then works the full ticket list independently and you can compare
their fixes ticket by ticket — the same shape as the validation round, where
each reviewer owned one CSV.

Each model gets the same handout as a solo coder (see [What to hand the
coder](#what-to-hand-the-coder)) with two lines changed:

> Use `--progress runs/<your model name>/PROGRESS.csv` on **every**
> `next_task.py` and `append_task.py` call. Commit into your own worktree only.

Name the run folder after the model, lowercase, the same way reviewers named
their CSVs — that is what makes the comparison readable afterwards.
`runs/` is gitignored, so the progress files stay out of the
commits you are comparing.

Two things this does **not** do, and neither is a tool problem:

- **It does not split the work.** Sharing one `tasks/PROGRESS.csv` between
  concurrent agents does not either: three agents that ask before any of them
  records all receive ticket 01, because nothing is claimed until an outcome is
  written. The loop is serial on purpose — that is what makes a context blow-up
  a resume instead of a data loss.
- **It does not isolate the working tree.** Several models committing into one
  checkout will collide. Give each its own clone or `git worktree`, and compare
  the branches afterwards.

---

## Ground rules for the run — give these to every model verbatim

They are the scoring criteria, and they are also printed inside every ticket's
`## Acceptance` section.

1. **Verify before fixing.** Grep the live source for the claimed defect. If it
   is already fixed or was never real, record `ALREADY-OK` and stop — do not
   produce a patch.
2. **One local commit per ticket.** Never `git push`.
3. **Every fix ships a regression test** in `tests_bugfix/` that fails without
   the change. If you add a `tests/test_*.py`, run `python3 scripts/sync_test_tiers.py`
   so the `.smoke_tests/` mirror exists (a pre-commit hook enforces it).
4. **Run the suite as four separate invocations** — combining the roots in one
   `pytest` call produces ~362 false errors from a conftest collision:
   ```bash
   for d in tests tests_bugfix .smoke_tests .regression_tests; do python3 -m pytest "$d" -q --timeout=180; done
   ```
   `python` is not on PATH — use `python3`.
5. **Do not adjust a test to make a change pass** unless the test pins a contract
   the change deliberately replaces — and say so explicitly in the commit if it does.
6. **Stay in scope.** No opportunistic refactors bundled into an unrelated fix.

---

## Handing the work back

The default is that each fix is a commit on the model's **own branch or
worktree**, and you compare branches directly afterwards
(`git log runs/<model>..`, `git range-diff`). Nothing to export.

If you deliver the fixes as **patch files** instead — portable, no shared
checkout needed — generate them one per commit into a per-model folder, then
prefix every file with the model name:

```bash
git format-patch -o runs/<model>/patches/ <base>..HEAD
cd runs/<model>/patches && for f in [0-9]*.patch; do mv "$f" "<model>-$f"; done
```

The model name goes in **both** the folder and the filename — the same way each
reviewer owned `validation-v1-<model>.csv` in stage 2. `git format-patch` numbers
the files `0001-…`, `0002-…` and every model produces the same names, so without
the prefix the files collide the moment two models land in one folder to be
compared, and a bare `0004-fix-….patch` on your disk no longer says who wrote it.

Prefix only — `<model>-0001-…`, `<model>-0002-…`. The `NNNN-` stays in sort
position, so a glob still lists the set in order and `git am` still applies it in
order:

```bash
git am runs/<model>/patches/<model>-*.patch      # applies the whole set, in order
```

Do not renumber or drop the `NNNN-` block; that is the part `git am` orders on.

`runs/` is gitignored, so the patches and progress files stay out of the commits
you are comparing.

---

## After the round

For every `FIXED` row in `tasks/PROGRESS.csv`:

- set that finding's `truth` to `FIXED` in `validate1/truth.csv` with the commit
  sha in `how`;
- add a row to the ground-truth table in `GROUND-competition.md` — that table is
  the only thing carried into the next round.
