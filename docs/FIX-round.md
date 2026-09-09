# Fixing the tickets — the stage-5 loop

Stage 4 (`scripts/make_jira_tasks.py`) turned the adjudicated ground truth into
one ticket per confirmed defect under `tasks/`. This is how a coder model works
through them.

There are **two ways to run it**. Both use the same `tasks/*.md` tickets and the
same ground rules; they differ only in how the coder is handed the next one.

---

## Variant 1 — you drive the loop (the old way)

Feed the model your fix-round instruction plus `tasks/INDEX.md`, and tell it to
take the tickets in order, one commit each, then move to the next line. This is
unchanged — nothing here replaces it.

---

## Variant 2 — a script hands out the next ticket (resumable)

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

## After the round

For every `FIXED` row in `tasks/PROGRESS.csv`:

- set that finding's `truth` to `FIXED` in `validate1/truth.csv` with the commit
  sha in `how`;
- add a row to the ground-truth table in `GROUND-competition.md` — that table is
  the only thing carried into the next round.
