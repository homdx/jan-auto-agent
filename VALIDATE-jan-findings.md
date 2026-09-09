# Validating jan's findings — prompts for reviewer models

`docs/TASK-jan-selfaudit.md` produces `IMPROVEMENTS.md`: a list of *proposed* defects.
This file turns that into a graded result you can compare across reviewer models.

Each reviewer gets: the **core prompt**, one **variant block**, that variant's
`IMPROVEMENTS.md`, and read access to the repo. It writes findings one at a time
into its own CSV.

## Where the prompt is — what to paste into a reviewer model

The prompt lives in **this file**. There is no separate prompt file. Assemble it
by copying two blocks of text from below, in order:

1. **`## Core prompt — prepend to every variant`** — the whole block-quoted body
   under that heading. Same for every reviewer, every run.
2. **One `### Variant N` block** — pick the variant that produced the list you
   are handing over. The current round is **Variant 1 — mutable state escaping
   accessors** (every entry is an aliasing / defensive-copy task).

Then attach, in the same message:

3. **The findings list**: `validate1/IMPROVEMENTS.md` (for this round). The
   reviewer does **not** open it directly — it is fed one entry at a time by
   `scripts/next_finding.py` — but the file must be present in the working tree.
4. **Read access to the repo.** The reviewer runs no code beyond a throwaway
   `python3 -c` / grep to check its own claim, and it touches nothing under
   `jan` / `main.py` — that was stage 1 and is already done.

The reviewer runs inside whatever agent shell you like (Kilo plugin, etc.); it
only needs a terminal in the repo root and the two loop scripts
(`scripts/next_finding.py`, `scripts/append_finding.py`).

### If `next_finding.py` says the list is "thin"

> `error: every entry in validate1/IMPROVEMENTS.md has no Location and no Instruction`

That means the `IMPROVEMENTS.md` copy was truncated to headings only (stage 1
sometimes renders just the `### ID: title` line and the `**Cluster:**` line).
A reviewer cannot judge a title alone, so the tool refuses it. Recover it one of
two ways:

- **Regenerate from stage 1** — the real, full list:
  ```bash
  python3 main.py --auto "<goal from docs/TASK-jan-selfaudit.md>" --dry-run \
      --config agents_128k.ini --base .
  cp IMPROVEMENTS.md validate1/IMPROVEMENTS.md
  ```
- **Or** pass `--allow-thin` to review titles only (weaker — records that the
  input was thin).

The `validate1/IMPROVEMENTS.md` in the tree now carries `**Location:**` /
`**Target files:**` / `**Instruction:**` for all 53 entries, so it passes as-is.

> **Revision 2**, after the first three-model run on Variant 1. What changed and
> why is in "What the first run showed" at the end — read it if you are tuning
> these prompts further.

---

## Core prompt — prepend to every variant

> You are auditing a list of proposed code defects against the live source. The
> list was produced by an automated agent and **its claims are unverified** —
> treat every entry as a hypothesis, not a finding.
>
> ### The loop — you do not get the list, you get one entry at a time
>
> Do not open `IMPROVEMENTS.md` yourself. Do not read ahead. Two commands drive
> this task, and you alternate between them until the queue is empty:
>
> ```bash
> # 1. ask for the next entry
> python3 scripts/next_finding.py --improvements IMPROVEMENTS.md \
>                                 --out validation-v<N>-<yourname>.csv
>
> # 2. judge that one entry, then record it
> python3 scripts/append_finding.py --out validation-v<N>-<yourname>.csv \
>     --variant <N> --task-id <the id you were just given> --title "..." \
>     --file <the path the entry cites> --symbol <ClassName.method> \
>     --verdict CONFIRMED --severity MEDIUM --defect-class mutable-state-leak \
>     --caller-mutates YES \
>     --impact "..." --repro "..." --evidence "..." --disproof "..." \
>     --fix-type code --effort S --confidence HIGH
> ```
>
> Then back to step 1. The queue is computed from your CSV, so **an entry you
> have not recorded is handed to you again** — running step 1 twice in a row
> gives you the same entry, not the next one. There is no way to get ahead of
> your own file, and nothing you decide exists until it is written.
>
> Judge exactly one entry per cycle. Do not batch, do not "check a few and write
> them up after" — the write is what ends the cycle. Assume this session can be
> cut off at any moment: whatever is not on disk is lost, and only what is on
> disk is graded. A finding you described in prose but never recorded counts as
> not done.
>
> `next_finding.py` prints `progress: N/M recorded, K remaining` each time. That
> number is your only measure of progress; if it is not moving, you are not
> working. **These lists are long — 50+ entries is normal.** Keep cycling.
>
> ### Your job ends at the CSV
>
> You are reviewing, not fixing. **Do not edit any source file, do not write or
> run tests, do not commit anything.** Read the code, decide, record the row.
> Fixing the confirmed defects — with a regression test per fix, run against all
> four test roots, one commit each — is a separate round with its own
> instruction (`docs/FIX-round.md`), and it is scored on ground truth that does
> not exist yet while you are working. A reviewer that patches the tree
> invalidates the comparison every other reviewer is being measured against.
>
> The one thing you may run is a throwaway check of your own claim — a
> `python3 -c "..."` identity test, a grep. Keep it out of the repo and put what
> it showed into `--evidence` or `--disproof`.
>
> ### Name your file after yourself, once
>
> Use `--reviewer <your model name, lowercase>` and let the helper build the
> filename, or pass the same `--out` every single time. Write it into the round's
> folder (`--out validate<N>/validation-v<N>-<yourname>.csv`), not the repo root
> — that folder is what the analysis scripts glob. Two spellings that
> differ only in case (`Kilo.csv`, `kilo.csv`) become two files, splitting your
> work and defeating the duplicate check, which is per-file. The tools now
> refuse the second spelling rather than start a rival file.
>
> Name yourself by **model**, not by the machine's user account and not by the
> tool you are running inside: `glm-4.5-flash`, `agnes-2-5`, `step-3-7-flash`.
>
> ### Running out of room is fine — stopping silently is not
>
> If you are near the end of what you can hold, stop cleanly and say how far you
> got: `progress: 17/53`. Because the queue is derived from your CSV, the run
> can be resumed later with the identical command — `next_finding.py` will hand
> over entry 18. Nothing already recorded is repeated or lost. What is not fine
> is trailing off without recording the entry you were working on.
>
> When it prints "The list is finished" (exit code 3), stop. Report your verdict
> counts and end the task. Do not re-audit anything — it is already on disk.
>
> If `append_finding.py` rejects a row, it names the one thing missing. Fix that
> and call it again; do not re-judge the entry from scratch. If it says a
> finding is already recorded, that entry is done — go to step 1.
>
> `--file` must be a path that exists in the repo. It is the key every reviewer's
> verdict on the same symbol is grouped under, so a placeholder path does not
> just weaken your row — it hides it from the comparison, and your judgement is
> read as something only you saw. Pass the path you actually opened.
>
> Anything you find that is *not* on the list gets recorded the same way, with
> `--task-id NEW-1`, `NEW-2`, …
>
> ### How to judge one entry
>
> Open the cited file, find the cited symbol (**by name — the line numbers in
> the list are stale**), read it, and decide what is true *now*.
>
> **1. Try to disprove it first.** Before you confirm anything, ask: *what one
> fact, if true, would make this a non-issue?* Then check that fact and write
> what you found into `--disproof`. This field is required for `CONFIRMED` and
> `ALREADY_FIXED`, and it is the difference between reviewing and agreeing.
>
> A worked example. A proposal says `get_progress` is a defect because
> `dict(self._progress)` is a shallow copy. The disproving fact: *a shallow copy
> is only a leak if the dict holds nested mutable values.* Grep every write to
> `_progress` — if they are all strings and ints, the copy is complete and the
> verdict is `FALSE_POSITIVE`. Recognising the pattern is not the job; checking
> whether its precondition holds here is the job.
>
> **2. Evidence or it didn't happen.** A `CONFIRMED` needs `--evidence` quoting
> the line that proves it, copied from the file. No quotable line means
> `UNVERIFIABLE`. Do not describe code you have not opened.
>
> **3. Separate reachable from latent.** `--caller-mutates YES` means you found
> a caller today that actually does the dangerous thing; name it in `--notes`.
> `NO` means the code is shaped badly but nothing exercises it. `CRITICAL` and
> `HIGH` require `YES` — a defect nothing can reach is `MEDIUM` at best, and
> saying so honestly is worth more than inflating it.
>
> **4. A defect needs a consequence.** If you cannot name specific inputs or
> state leading to a specific wrong result, severity is `NONE` however untidy
> the code looks. Style is not a defect.
>
> **5. `ALREADY_FIXED` is a real answer.** If the defect was once real and the
> code now guards against it, say so and name the guard in `--evidence`. Finding
> that a list is out of date is a result, not a failure.
>
> **6. Report what the list missed.** A defect of the same class that the list
> did not mention gets a row with `--task-id NEW-1`, `NEW-2`, … This is the
> highest-value output of the exercise.
>
> **7. Let the queue tell you when to stop.** `next_finding.py` exits 3 with
> "The list is finished" once every entry is recorded. Report your verdict
> counts then and stop — do not keep exploring the codebase for more, and do not
> revisit entries. A run that confirms everything is a run that checked
> nothing.

### CSV columns

Written by the helper — listed here so you know what to supply.

| Column | Values | Meaning |
|---|---|---|
| `variant` | `1`–`6` | Which self-audit variant produced the list |
| `task_id` | `AUTO-T3`, `NEW-1` | From IMPROVEMENTS.md, or `NEW-n` for your own find |
| `title` | free text | Short imperative phrase |
| `file` | repo-relative path | The file you actually opened — required |
| `symbol` | function/class | Prefer this over line numbers |
| `line_start`,`line_end` | integers | **As they are now** |
| `verdict` | see below | |
| `severity` | `CRITICAL`…`NONE` | By consequence, not by appearance |
| `defect_class` | slug | `mutable-state-leak`, `silent-except`, `regex-boundary`, `vacuous-test` … |
| `caller_mutates` | `YES`/`NO`/`UNKNOWN` | Does a caller today actually do the dangerous thing? |
| `impact` | free text | What breaks, and when |
| `repro` | free text | Concrete inputs/steps, or `none` |
| `evidence` | quoted code | Required for `CONFIRMED` |
| `disproof` | free text | The check that would have falsified this, and its result |
| `fix_type` | `code`/`test`/`docs`/`config`/`none` | |
| `effort` | `S`/`M`/`L` | S ≤ 1 file, M = 2–3, L = cross-cutting |
| `confidence` | `HIGH`/`MEDIUM`/`LOW` | |
| `notes` | free text | Name the mutating caller here when `caller_mutates=YES` |

**Verdicts.** `CONFIRMED` — present now, with evidence. `FALSE_POSITIVE` — the
claim is wrong about what the code does. `ALREADY_FIXED` — real once, guarded
now. `UNVERIFIABLE` — citation does not resolve, or too vague to test.
`OUT_OF_SCOPE` — real, but a different defect class than this variant asked for.

**Severity.** `CRITICAL` aborts a run, corrupts persisted state, or silently
produces a wrong result the user acts on · `HIGH` wrong behaviour in a reachable
case · `MEDIUM` degrades a documented guarantee, or is real but unreachable
today · `LOW` bounded — a missing log line, a bad message, wasted work · `NONE`
no describable consequence.

---

## Variant blocks — append one to the core prompt

### Variant 1 — mutable state escaping accessors

> The list came from a search for accessors that return a mutable object from
> internal state, letting a caller mutate the owner's state without validation.
>
> **In Python every attribute is mutable, so the shape alone proves nothing.**
> A finding needs all three: the object is reachable from outside the class, a
> mutation through it changes the owner's state, and that state is *used* —
> persisted, or read back as a decision.
>
> Not findings: a plain instance attribute a caller could reassign (that is how
> objects work); a dataclass field on a value object that is constructed and
> passed once; a module-level private a test fixture resets. Say
> `OUT_OF_SCOPE` or `NONE` and move on — do not spend a row arguing.
>
> Check both directions. `--caller-mutates YES` needs a real caller doing it;
> otherwise it is latent. And check one level down: a shallow copy that shares
> nested lists or dicts is the same defect — **but only if nested mutable values
> actually exist**, which is the disproof step.

### Variant 2 — fail-open handlers that hide their failure

> The list came from a search for `except` blocks that swallow an exception
> without logging what was swallowed.
>
> This codebase is *deliberately* fail-open in many places — continuing past an
> error is often the correct architecture, and a proposal to convert a handler
> into a raise is a `FALSE_POSITIVE` unless the swallowed error makes the
> program produce a wrong answer rather than a degraded one. You are grading
> **visibility**: after this handler runs, can an operator tell something
> failed? `LOW` when the failure is cosmetic, `HIGH` when a silent skip changes
> the result the user sees.
>
> Disproof for this class: does something *else* on the path already log it —
> the caller, a wrapper, a `finally`? Check before confirming.

### Variant 3 — regex boundaries the content can forge

> The list came from a search for regexes that parse structured documents by
> matching a boundary the document body could itself contain — markdown
> headings, code fences, horizontal rules, delimiters.
>
> To confirm one, construct the forging input: a document whose *body* contains
> the marker. Put it in `--repro` and say what the regex does with it.
>
> Disproof for this class: who writes the input? If it is machine-generated with
> a guaranteed shape and no LLM-authored text reaches it, the ambiguity is
> theoretical — `LOW` at most, and say who the writer is in `--notes`.

### Variant 4 — unvalidated data reaching disk

> The list came from tracing writes to JSON state files for paths where data
> arrives without the schema validation the module's own setters apply.
>
> Trace the whole path from entry to serialisation and name every gate it passes.
> A write is a defect only if you can name the bypassed gate **and** show a value
> that would survive the bypass.
>
> Disproof for this class: were all callers validated upstream? If so the honest
> verdict is `FALSE_POSITIVE` with the upstream gate named in `--evidence`.
> "It writes without validating" is not a finding on its own.

### Variant 5 — tests that cannot fail

> The list came from a search for tests that would still pass if the code they
> guard were deleted.
>
> Verify by mutation: name the specific production change that *should* make
> this test fail, put it in `--repro`, then read the test and decide whether it
> would. A test asserting over a collection that is empty in the fixture, or
> comparing to a set that could silently become empty, is `CONFIRMED`.
>
> `HIGH` when the test is the only guard on a real invariant — a vacuous guard
> is worse than no guard, because it reports coverage that does not exist.
>
> Disproof for this class: is there a sibling test that *would* catch the
> mutation? Then this one is redundant, not vacuous — `LOW`.

### Variant 6 — cross-module contract drift

> The list came from a search for places where one module documents a guarantee
> about another module's behaviour that the other may no longer provide.
>
> Read both halves. Quote A's claim **and** B's current behaviour in
> `--evidence`, and say in `--notes` which side is wrong — the comment or the
> code. A stale comment is `LOW` unless something reads it as a contract; if a
> caller relies on the documented behaviour, `HIGH`.
>
> Disproof for this class: is the comment describing an intent rather than a
> guarantee? Aspirational comments are not contracts.

---

## Running it across several models

1. One CSV per model per variant: `validation-v<N>-<model>.csv`, written one
   row at a time by `append_finding.py` and paced by `next_finding.py`. Check
   progress at any point without disturbing the reviewer:
   ```bash
   python3 scripts/next_finding.py --improvements IMPROVEMENTS.md \
                                   --out validation-v1-model.csv --status
   ```
2. Merge and compare:
   ```bash
   python3 scripts/merge_validations.py validation-v1-*.csv
   ```
3. **A reviewer that stops early is resumable.** Re-run the same model with the
   same command and the same CSV; the queue picks up where it stopped. Two or
   three sessions to finish a 53-entry list is normal and costs nothing extra —
   the first run's work is already on disk.
4. **Build the report and score the reviewers:**
   ```bash
   python3 scripts/harvest_report.py validate1/*.csv \
       --truth validate1/truth.csv \
       --report harvest.md --actions actions.csv --solo solo.csv
   ```
   `harvest.md` sorts findings into **Act / Disputed / Already fixed /
   Dismissed**, adds a **Solo findings** table, and scores each reviewer.

### Judging which model is better

Skepticism alone does not rank reviewers. On a noisy list everybody scores 90%+
by rejecting nearly everything, which is the correct behaviour and therefore not
a discriminator. Rank on two things instead, in this order:

**1. Accuracy against ground truth.** Keep a `truth.csv` of findings you have
personally checked:

```csv
finding,truth,checked_by,how
tools/search_agent.py::_DEFAULT_SKIP_DIRS,REAL,you,"identity check: instance list is the module list"
tools/metrics_collector.py::MetricsCollector._load_all_cached,FALSE,you,"sole caller copies before mutating"
tools/auto/state.py::StateStore.get_task,FIXED,you,"returns self._detached(t) since 4796e0a"
```

`REAL` / `FALSE` / `FIXED`. Only score what you actually verified — an unchecked
finding is left out of the maths rather than counted as a pass, so a reviewer
cannot gain by guessing. Nine checked findings were enough to separate the field
in practice.

**2. Solo discoveries that survive checking.** A `NEW-*` row only one reviewer
produced is the highest-value output of the whole exercise, *and* the highest-
risk: it is either the best result of the run or a hallucination, with nothing in
between. The report puts these in their own table so you check them by hand.

**A single vote is weak evidence about the finding and strong evidence about the
reviewer.** Do not dismiss a solo find for being solo — nobody else was looking
there. Check it, then add the answer to `truth.csv`, which raises the resolution
of every future run.

### Reading the solo table

It separates two things that must not be averaged:

- **Discoveries** — `NEW-*` rows nobody else raised. Judgement signal.
- **Unshared judgements** — list entries nobody else reached. Coverage gap, and
  says nothing about anyone's ability. Re-run a second reviewer over those ids
  before trusting a lone verdict.

### Grade the reviewers, not only the findings:
   - **Unanimous `CONFIRMED` with matching evidence** → trust the finding.
   - **Split verdicts** → the interesting rows; one reviewer read the code and
     the other read the proposal.
   - **All-`CONFIRMED` reviewer** → it did not verify. The share of
     non-`CONFIRMED` verdicts is a quality signal by itself.
   - **`NEW-*` rows** → either the best result of the run or a hallucination.
     There is no third option; check them.

---

## What the first run showed

Three models on Variant 1, 54 / 54 / 46 rows. What went wrong drove revision 2.

**Nobody wrote incrementally, however firmly they were asked.** Revision 1 said
"record each finding the moment you decide it" and every model still judged in
bulk and wrote at the end. Instruction was the wrong instrument: revision 2
takes the list away and hands out one entry at a time, computed from the CSV, so
an unrecorded entry is simply handed back. The write is now the only way to make
progress rather than a step that can be deferred.

**One model looped and its file was unusable.** All 46 of its rows had the wrong
column count — 12, 13, 27 where 17 were expected — because it hand-formatted CSV
and unquoted commas fused records together. It also repeated 7 findings it had
already judged. Both are now structurally impossible: `append_finding.py` owns
the formatting and refuses a duplicate `file`+`symbol`.

**All three confirmed a false positive.** Every model flagged
`StateStore.get_progress` (`return dict(self._progress)`) as a shallow-copy leak
— `CONFIRMED`, severity `MEDIUM`. But `_progress` only ever holds strings and
ints, so `dict()` is a complete copy and there is nothing to leak. They matched
the pattern without checking its precondition. Hence the mandatory `--disproof`
field, and that exact case as the worked example in the core prompt.

**Severity was inflated by treating Python as if it were not Python.** Both
clean runs opened with `AutoController.config returns a live ConfigParser` and
`CandidateTask.target_files exposes a mutable list` at `HIGH` — a plain
attribute and a dataclass field. Hence `--caller-mutates`, the `HIGH`/`CRITICAL`
gate that depends on it, and Variant 1's explicit "not findings" list.

**The calibration worked.** `step3-7-flash` correctly returned `ALREADY_FIXED`
for `get_task` and `resume_info`, citing `_detached()` and the regression test —
against a repo where those had been fixed hours earlier. That is the behaviour
the calibration run exists to detect, and it is the reason to keep running
Variant 1 against a commit whose answer you already know.

## What the second run showed

Four reviewers against a 53-entry list, using the queue.

**The structural fixes held.** Zero malformed rows across all four files (the
previous run had one model at 46 malformed out of 46), and zero rows missing
`disproof`. Nobody hand-formatted CSV because nobody could.

**The disproof field caught the false positive that had fooled everyone.** In
run 1 all three models confirmed `StateStore.get_progress` as a shallow-copy
leak at `MEDIUM`. In run 2 it came back `FALSE_POSITIVE / NONE` with the reason
written out: *"Searched all writes to self._progress: only scalar assignments
(str/int). No nested lists, sets..."* — the precondition checked instead of the
pattern matched.

**The severity gate held.** No `HIGH` or `CRITICAL` anywhere; everything landed
`MEDIUM` or `NONE`, which is the honest reading of a class of latent shape
issues that no caller exercises.

**Nobody finished.** Best coverage was 17/53; others managed 4, 2 and 1. This is
now a resumption problem rather than a data-loss problem — every judged entry is
on disk, so re-running the same model continues from where it stopped. Hence the
explicit note above that long lists are expected and that stopping cleanly with
a progress number is an acceptable outcome.

**One reviewer split itself in half.** The same model wrote `Kilo.csv` and
`kilo.csv` on alternating calls, halving its own coverage and bypassing the
per-file duplicate check. Both tools now refuse a filename that differs from an
existing one only in case, and `--reviewer` builds the name so it cannot drift.
