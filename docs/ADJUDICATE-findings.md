# Stage 3 — adjudicating the open questions

Stage 2 asked several reviewers *"is this proposal true?"* and produced
`pending-validation.md`: findings at least one reviewer confirmed that nobody has
checked against the code. Those are **questions, not results**.

Stage 3 settles them. Its output becomes `truth.csv` — the scoring key for every
reviewer and every future run — so the bar is higher than stage 2: you are no
longer judging a claim, you are judging the code.

Give each adjudicator this prompt, the `pending-validation.md` from the round,
and read access to the repo. Optionally give them the stage-2 reviewer CSVs so
they can see who said what; it is not required, and there is an argument for
withholding them so the adjudicator forms its own view first.

---

## Adjudicator prompt

This stage is **script-driven only** — there is no by-hand variant. The
adjudicator loops on `next_pending.py` / `append_verdict.py`, one finding at a
time, exactly like stage 2.

**▼▼▼ PROMPT STARTS — paste everything block-quoted below, up to "PROMPT ENDS" ▼▼▼**

> You are settling open questions about whether reported defects are real. Each
> was confirmed by at least one automated reviewer and contradicted by none, or
> by some — either way nobody has read the code to decide. Your answer becomes
> ground truth, so an opinion is not admissible: only something you can point at.
>
> ### The loop
>
> ```bash
> # 1. take the next unsettled question
> python3 scripts/next_pending.py --pending pending-validation.md \
>                                 --out truth-<yourname>.csv
>
> # 2. read the code, then record the answer
> python3 scripts/append_verdict.py --out truth-<yourname>.csv \
>     --finding "<the file::symbol you were just given>" \
>     --truth REAL --checked-by <yourname> --confidence HIGH \
>     --how "the check you ran" --evidence "the line that proves it" \
>     --consequence "what goes wrong, and when" --severity LOW \
>     --fix "one line on the shape of the fix"
> ```
>
> Then back to step 1. The queue is derived from your verdict file, so an
> unrecorded finding is handed back — running step 1 twice gives you the same
> question. Decide one, write it, move on. Stop when it says the queue is
> finished (exit 3) and report your counts.
>
> ### Deciding
>
> **`REAL`** — the defect is present in the code as it stands now. Quote the line
> in `--evidence` and say in `--consequence` what specifically goes wrong and
> when. A defect no caller can reach is still `REAL`, but its severity is `NONE`
> or `LOW` and you should say which — do not inflate a latent shape into a live
> bug, and do not dismiss it either.
>
> **`FALSE`** — the code does not do what the report says. This is the most
> valuable verdict you can give, because it is the one an automated reviewer
> cannot reach: it usually requires reading a *caller*, not the cited symbol. A
> worked example from a previous round — a reviewer flagged
> `_load_all_cached` for returning the live cache list; the sole caller does
> `records = list(records)  # don't mutate the cached list in place`, so there
> was no defect. Reading one call site settled it.
>
> **`FIXED`** — real once, guarded now. Quote the guard.
>
> **`UNDECIDED`** — you genuinely cannot tell without running something you
> cannot run. Honest, and better than a coin flip. Say what would settle it.
>
> ### What `--how` must contain
>
> The check you ran, not the conclusion you reached. "Looks unsafe" is not a
> check. "Grepped every write to `_progress`: all scalars, so `dict()` is a
> complete copy" is a check. `--how` is what a human reads when your verdict is
> the one out of step with everyone else's, and it is what makes this file worth
> keeping after you are gone.
>
> ### Two habits worth borrowing
>
> **Read the callers, not just the symbol.** Most false positives in this
> codebase are shapes that look wrong in isolation and are neutralised one frame
> up. Nothing about the cited line will tell you that.
>
> **Try to disprove before you confirm.** Ask what one fact would make this a
> non-issue, then go and check that fact specifically. It is faster than
> confirming, and it is what separates the adjudicators that were right from the
> ones that agreed.

**▲▲▲ PROMPT ENDS — paste everything above, back to "PROMPT STARTS" ▲▲▲**

---

## After the adjudicators finish

```bash
# merge into one ground truth; exit 4 if anything is still disputed
python3 scripts/truth_consensus.py validate<N>/truth-*.csv \
    --truth validate<N>/truth.csv --report validate<N>/adjudication.md

# re-score the stage-2 reviewers against the settled answers
python3 scripts/harvest_report.py validate<N>/validation-*.csv \
    --truth validate<N>/truth.csv --report validate<N>/harvest.md

# turn the confirmed defects into tickets
python3 scripts/make_jira_tasks.py --truth validate<N>/truth.csv \
    --findings 'validate<N>/validation-*.csv' \
    --verdicts 'validate<N>/truth-*.csv' --out tasks/
```

**Disputes are not resolved by majority.** The code either does the thing or it
does not; a split means one side did not run the check it claims. Read those
yourself and record the answer with `append_verdict.py --checked-by you`.

**Aliases.** The same defect often arrives at several symbols — the constant,
the constructor that binds it, the attribute it lands on. Add a `duplicate_of`
column to `truth.csv` naming the canonical finding, and the ticket generator
folds them into one ticket instead of filing the same work three times.

**Then update the ground truth table in `GROUND-competition.md`.** The CSVs are
run data and are gitignored; that table is the part that survives, and every
answer added to it raises the resolution of every future run.
