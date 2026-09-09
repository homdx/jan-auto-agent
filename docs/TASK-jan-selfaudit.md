# Self-audit task for jan-auto-agent

Run jan against its own source to see what it can find. Every run below is
**plan-only**: `--dry-run` builds the plan and writes `IMPROVEMENTS.md`, and
executes nothing and commits nothing.

## How the goal steers the search

The goal string is interpolated into the Architect's prompt as *"produce tasks
that IMPLEMENT the goal '{goal}'"*. Three consequences worth designing around:

- **Phrase it as an imperative defect class, not a question.** "Find bugs" reads
  as a feature request; "Replace X with Y wherever Z" reads as work.
- **Narrow beats broad.** The Architect sees 6 files per review
  (`max_files_per_review`) and emits at most 5 tasks per cluster. A vague goal
  spends that budget on type hints and docstrings.
- **Grounding is enforced.** Every task must cite a real file plus a symbol or
  line range, and Gate 1 re-checks that the cited problem is actually present.
  A goal describing a *shape* the model can look for ("a method that returns a
  dict held inside self._plan") grounds better than one describing a *feeling*
  ("improve robustness").

## The two-pass protocol

Never judge a run by `IMPROVEMENTS.md` alone — it is the raw proposal list.

```bash
# pass 1 — propose
python3 main.py --auto "<GOAL>" --dry-run --config agents_128k.ini --base .

# pass 2 — jan re-checks its own proposals against the live code
python3 main.py --validate-plan --config agents_128k.ini --base .
```

Pass 2 re-runs each task through the same Gate 1 existence + problem-presence
check and moves anything it can no longer confirm into `IMPROVEMENTS-FALSE.md`.
**The survivors are the result.** The ratio between the two files is itself a
measurement: it tells you how much of what jan proposes it cannot re-confirm
ten minutes later.

Back up both files between runs — each run overwrites them:

```bash
cp IMPROVEMENTS.md IMPROVEMENTS-$(date +%H%M)-<variant>.md
```

---

## Variant 1 — Known-answer test (run this first)

**There is a real, unfixed defect of exactly this class in `tools/auto/state.py`.**
`StateStore.get_task()` returns the live dict out of `self._plan`; a caller can
mutate it and the next validated setter persists the corruption. Confirmed by
repro: a non-enum `status: 12345` reaches `plan.json`.

```bash
python3 main.py --auto "Find every accessor method that returns a mutable object taken directly from internal state instead of a copy, and change it to return a copy. A caller that mutates the returned object corrupts the owner's state without going through any validation." --dry-run --config agents_128k.ini --base .
```

**This is the calibration run.** You already know the answer, so the result
grades the agent rather than the codebase:

- Finds `get_task` → the pipeline works end to end
- Finds only `all_tasks` (a shallow copy, a lesser instance of the same bug) →
  partial credit, it has the concept but not the sharpest case
- Finds neither but proposes plausible-looking work elsewhere → the failure mode
  worth knowing about, because every other variant's output is then suspect

Do this one before trusting any of the rest.

---

## Variant 2 — Fail-open handlers that hide their own failure

jan is deliberately fail-open in many places, which is correct — but a handler
that swallows an exception and logs nothing is indistinguishable from success.

```bash
python3 main.py --auto "Find except blocks that swallow an exception without logging what was swallowed, and add a warning that names the operation and the exception. Do not change control flow: a handler that intentionally continues must keep continuing, it must only become visible." --dry-run --config agents_128k.ini --base .
```

The explicit "do not change control flow" clause matters. Without it the model
proposes converting fail-open handlers into raises, which would break the
architecture on purpose.

---

## Variant 3 — Regex parsing of structured text

The `plan_validator` section-removal bug was this class: a regex boundary that a
document's own content could forge. Fenced code blocks, markdown headings and
`---` separators are all forgeable by an LLM writing an instruction.

```bash
python3 main.py --auto "Find regular expressions that parse structured documents by matching a boundary marker that the document body could itself contain — markdown headings, code fences, horizontal rules, delimiters. Make each one aware of the enclosing structure so content inside a fenced block is never treated as a boundary." --dry-run --config agents_128k.ini --base .
```

---

## Variant 4 — Unvalidated data reaching disk

```bash
python3 main.py --auto "Trace every write to a JSON state file and identify paths where data reaches the file without passing the schema validation that the module's own setters apply. Add validation at the boundary or document why the write is already safe." --dry-run --config agents_128k.ini --base .
```

Overlaps Variant 1 by design — running both and comparing shows whether jan
finds the same defect from two different framings, which is a much stronger
signal than one hit.

---

## Variant 5 — Tests that cannot fail

The highest-value class, and the one a code-review model is least likely to
raise unprompted: a guard asserting a condition that holds vacuously.

```bash
python3 main.py --auto "Find tests that would still pass if the code they guard were deleted — assertions over empty collections, checks whose subject is always absent, or a comparison to a set that could silently become empty. For each, add a companion test proving the guard fails when the invariant is actually violated." --dry-run --config agents_128k.ini --base .
```

---

## Variant 6 — Cross-module contract drift

Cheap to run, and targets the one thing single-file review cannot see.

```bash
python3 main.py --auto "Find places where one module documents a guarantee about another module's behaviour in a comment or docstring, and verify the other module still provides it. Report each mismatch as a task to correct whichever side is now wrong." --dry-run --config agents_128k.ini --base .
```

---

## Tuning, if the first runs come back thin

| Symptom | Knob | Where |
|---|---|---|
| Tasks cite the right file but the wrong symbol | `probe_enabled = true` | `[architect]` — lets it request more context before deciding; costs tokens |
| Only 1–2 tasks per cluster | `max_files_per_review` 6 → 8 | `[architect]` — wider view, fewer clusters |
| Truncated JSON in the log | `max_tokens` 16384 | `[architect]` — already high; shrink `max_files_per_review` instead |
| Everything lands in `IMPROVEMENTS-FALSE.md` | goal too abstract | Rewrite it as a shape to look for, not a quality to improve |

## What to record per variant

- Tasks proposed (`IMPROVEMENTS.md`) vs survived (`IMPROVEMENTS-FALSE.md` delta)
- Whether Variant 1 found `get_task`
- Any finding that is *true* and that you had not already found by hand — that
  is the only number that says the agent earns its tokens
