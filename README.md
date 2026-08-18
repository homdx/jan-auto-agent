# jan-auto-agent — Pipeline Overview

This document describes the four operating modes of `main.py`, what each
stage of the pipeline actually does, and the anti-hallucination /
resilience defenses built into it. If you only want the `--collect`
artifact reference (`.collect/` file formats), skip to
[Collect artifact reference](#collect-artifact-reference) at the bottom —
that section is unchanged from the original COLLECT-25 notes.

## The four commands

```bash
# 1. Build the structural project model (optional but recommended first)
python3 main.py --base ../test7 --collect --config agents_128k.ini

# 2. Make a plan — Architect proposes tasks, Gate 1 filters them, nothing is executed
python3 main.py --auto "improve error handling across the codebase" \
    --base ../test3 --dry-run --config agents_128k.ini

# 3. Re-validate an existing plan against the CURRENT code (catches drift/false positives)
python3 main.py --validate-plan --base ../test1 --config agents_128k.ini

# 4. Run it for real — same planning pipeline, then executes + commits each task
python3 main.py --auto "improve error handling across the codebase" \
    --base ../test3 --config agents_128k.ini
```

These four map directly onto the pipeline stages below. `--dry-run` and a
full `--auto` run share the *exact same* planning code path (ingest →
Architect → Gate 1 → backlog → plan emit); the only difference is that
`--dry-run` returns right after the plan is written, before Gate 2 /
execution ever starts.

## Pipeline stages

```
 ┌───────────┐    ┌───────────┐    ┌────────┐    ┌─────────┐    ┌──────────┐    ┌────────┐
 │  collect  │───▶│  ingest   │───▶│Architect│───▶│ Gate 1  │───▶│  plan    │───▶│ Gate 2 │
 │(optional) │    │(cluster   │    │(propose │    │(filter  │    │  .json + │    │(execute│
 │           │    │ repo)     │    │ tasks)  │    │ tasks)  │    │IMPROVE-  │    │ + val- │
 │           │    │           │    │         │    │         │    │MENTS.md) │    │ idate) │
 └───────────┘    └───────────┘    └────────┘    └─────────┘    └──────────┘    └────────┘
                                                       ▲
                                          --validate-plan re-runs
                                          this stage against an
                                          EXISTING plan.json
```

### 1. `--collect` — structural project model (optional, but recommended)

```bash
python3 main.py --collect --base ../test7 --config agents_128k.ini
```

One-shot, read-only pass over the source tree (`tools/collect/`). Builds a
structural model of the repo into `.collect/` — symbol tables, config-key
map, existing test coverage, cross-module contracts, a registry of
silently-swallowed exceptions, etc. (see
[Collect artifact reference](#collect-artifact-reference)).

This is **not** required to run `--auto`, but when `[collect] use_in_auto
= true`, both the Architect and Gate 1 get a `CollectBridge` that injects
grounding context per task — e.g. *"this file already has 90% test
coverage"* or *"this config key already has a documented fallback"* — which
measurably reduces false-positive task proposals. Re-run `--collect
--check` to see if the artifact is stale (source changed since last
build); `--refresh` forces a full rebuild; `--module <path>` patches one
file incrementally.

### 2. Ingest — clustering the repo

`tools/auto/repo_ingest.py` groups files into `RepoCluster`s (by directory /
pattern) so the Architect never has to see the whole repo in one prompt.
Large clusters are further split into **batches**
(`[architect] max_files_per_review`, default a handful of files) to stay
inside the model's context window — this is also the unit the
architect-checkpoint resumes at (see [Resumability](#resumability)).

### 3. Architect — proposing candidate tasks

`tools/auto/architect.py`, class `ClusterReviewer`. One LLM call per
cluster/batch, asked to produce up to `max_tasks` (default 5, or 1 in
creative mode) concrete `{title, instruction, target_files,
acceptance_check, cited_location}` objects that implement the goal against
the files it was shown.

**Defenses at this stage:**

- **Grounding requirement.** Every candidate must cite a real
  `cited_location` (file + symbol or line range) that resolves in the
  actual repo. Un-groundable candidates are dropped before ever reaching
  Gate 1 — no wasted Gate-1/Gate-2 attempts on a hallucinated citation.
- **Reserved-file guard.** A candidate can never target its own
  control/memory files (`plan.json`, `IMPROVEMENTS.md`, `story_bible.md`,
  …) — editing these would corrupt the pipeline's own bookkeeping.
- **Transient-error retry.** A 5xx / connection-refused / timeout on the
  LLM call itself is retried with configurable backoff
  (`[architect] retry_delays_sec`, default `5,15,30`), separate from
  everything below.
- **AUTO-H4 — shrink-retry on truncation.** If the JSON array comes back
  cut off mid-object (the model ran out of output tokens), the salvaged
  prefix is kept as a fallback, but the batch is **automatically re-asked
  with a smaller `max_tasks`** (`[architect] truncation_retry_max`,
  default 2 attempts; `truncation_shrink_factor`, default `0.5`) instead of
  just losing the truncated tail task. Each re-ask is a fresh, independent
  call — results are never merged across attempts, only the last
  attempt's outcome is kept.
- **Architect checkpoint** — see [Resumability](#resumability).

### 4. Gate 1 — filtering candidate tasks (before any code is written)

`tools/auto/gate1_filter.py`, class `Gate1Filter`. Runs on **every**
Architect candidate, in two steps, before anything is added to the plan:

1. **Existence check** — no LLM. Does the cited file/symbol actually exist
   in the repo right now? Mechanical, cheap, catches the obvious
   hallucinations immediately.
2. **Presence check** — one LLM call per surviving candidate. The model is
   shown the actual code at the cited location and asked: *"is the claimed
   problem actually present, and not already fixed?"*

**Defenses at this stage (this is where most of the anti-hallucination
work lives):**

- **Mandatory evidence quote.** A `"confirmed"` verdict must come with an
  `"evidence"` string that is a verbatim, whitespace-normalised substring
  of the code block shown. Missing, empty, or fabricated evidence is
  treated as an unsupported claim and downgraded to a rejection — a model
  cannot confirm a bug just by sounding confident about it. This is a
  mechanical check, not a judgment call, and applies to every production
  call site (`--auto` and `--validate-plan` alike).
- **Intentional-design note.** Before judging, Gate 1 scans the cited
  code's own comments/docstrings for language asserting the current shape
  is deliberate (`# noqa`, a `"Hardening:"` docstring line, `on purpose`,
  etc.) and surfaces it to the LLM as a counter-note — negation-aware, so
  `"NOT intentional"` / `"unintentional side effect"` correctly do *not*
  suppress a genuine bug report.
- **Test-helper note.** A candidate that proposes wrapping a private
  helper (`_foo`) inside a `.py` test file in try/except or input
  validation gets flagged — test helpers are conventionally meant to fail
  loudly, and "hardening" it usually makes debugging a broken fixture
  harder, not easier. Restricted to Python files by design (the
  leading-underscore convention doesn't generalise to other languages).
- **Re-ask on unparseable reply.** A response that isn't valid JSON with a
  recognised verdict is re-asked once with a stricter nudge before being
  treated as a genuine failure — this is separate from (and does not
  bypass) the evidence requirement above.
- **Collect-context notes** (when `[collect] use_in_auto = true`):
  existing test coverage and documented config-fallback notes, sourced
  from the `--collect` artifact rather than re-derived per call.

Rejected candidates never reach the plan; they're logged with a reason.

### 5. Plan emission

`tools/auto/backlog_prioritiser.py` + `tools/auto/plan_emitter.py`. Gate
1-accepted candidates become `plan.json` tasks (via `state.py`) and
`IMPROVEMENTS.md` (human-readable). If git is available, the plan is
committed. `--dry-run` stops here — this is the complete output of
`--auto ... --dry-run`.

### 6. `--validate-plan` — re-checking a plan against the CURRENT code

```bash
python3 main.py --validate-plan --base ../test1 --config agents_128k.ini
```

Requires a plan already on disk (built by a prior `--auto ... --dry-run`
or full run). Re-runs the **exact same** Gate 1 (`filter_candidates`) —
same evidence check, same grounding notes — against the code as it exists
*right now*. This exists because time passes between planning and
execution (or between plan and re-plan): a task can become stale if the
code has since changed, or turn out to have been a false positive Gate 1
missed the first time under a different model/config.

- Tasks that no longer confirm are **removed from `plan.json`** and logged
  to `IMPROVEMENTS-FALSE.md` (with a `_removed via --validate-plan_` note),
  not silently dropped.
- Tasks that still confirm are kept untouched.
- Does **not** build a plan and does **not** execute anything — it is
  purely a re-check step you can run any time between planning and
  execution.
- You can point `[gate1]` at a different (e.g. stronger/slower) model just
  for this re-check without touching the live `--auto` Gate 1 config.

### 7. Execution — Gate 2 (only in a full `--auto` run, not `--dry-run`)

`tools/auto/inner_loop.py`, class `InnerLoop`. For each task in the plan,
runs up to `max_attempts` (`[auto] max_attempts_per_task`, default 5)
rounds of:

```
coder (writes/fixes code) → executor (runs acceptance_check) → validator (LLM judges the result)
```

**Gate 2 requires BOTH halves to pass:**

1. **Objective half — executor.** Mechanically runs the task's
   `acceptance_check` (typically a shell command like `pytest
   tests/test_x.py`). Pass/fail by exit code, no LLM involved.
2. **Subjective half — validator (LLM).** Only called if the executor
   passed. Shown the task, the execution output, and the actual code
   change, and asked whether the implementation is complete and correct.
   Rejects come back as **structured feedback** — `reason`, `hints` (each
   required to point at a specific name/line/pattern in the code, not a
   generic "make sure it's correct"), and an optional
   `suggested_approach` — which is fed into the *next* attempt's context,
   so a rejected attempt isn't a dead end but a concrete correction.

If the executor fails, the validator is never called — there's no point
asking an LLM to judge output that's already objectively broken.

Only a task that passes **both** halves is committed. A task that
exhausts `max_attempts` without passing both stays `pending`/failed in
`plan.json` for the next run (or for a human to look at) rather than being
silently marked done.

## Resumability

**Execution (Gate 2 / task loop):** fully resumable by design. Task status
(`done` / `pending`) lives in `plan.json` under `<base>/.agent/`. A plan
that already exists on disk is never rebuilt (`plan_phase` short-circuits
with "plan already exists — skipping (resume)"); the task loop picks up
wherever `status != done` starts, respecting `[auto] max_runtime_min` and
`max_tasks_per_run` caps freshly on each run.

**Planning (Architect / Gate 1), including under `--dry-run`:** partial.

- `plan.json` is only written at the very end of planning (after Gate 1,
  backlog build, and emit). Ctrl+C *before* that point means the plan
  doesn't exist yet — on re-run, `has_plan` is false and the *entire*
  planning phase restarts.
- However, `tools/auto/architect.py`'s `review_clusters()` writes
  `<base>/.agent/architect_checkpoint.json` **incrementally, after each
  completed cluster/batch** — not just at the end. The cache key is a
  content-aware fingerprint of `{cluster/batch name, goal, file
  contents}`, so a re-run with the same `--base`/goal/config restores
  every already-completed batch from disk (no LLM call, near-instant) and
  only re-sends batches that hadn't finished at the time of interruption.
  A batch that failed outright (network error after all retries) is
  deliberately *not* cached, so it's retried, not skipped, on the next
  run.
- **Gate 1 (presence check) has no checkpoint.** If interrupted during or
  after Gate 1 but before the plan is emitted, the next run restores the
  Architect stage for free (per above) but re-runs Gate 1's presence-check
  LLM calls from scratch for every candidate.
- **`plan_validator`'s revision loop** (creative-mode plan feedback cycle)
  likewise has no persistence and restarts fully.

**Practical takeaway:** if you Ctrl+C a `--dry-run`, re-running the exact
same command is cheap for the batches the Architect had already finished,
but expect Gate 1 to redo its LLM calls for every candidate that had
already been produced. If you need Gate 1 to also resume incrementally,
that would need its own content-aware checkpoint (same pattern as
`architect_checkpoint.json`) — not currently implemented.

## What to do / what not to do

**Do:**
- Run `--collect` once (and re-run `--collect --check` periodically) on
  any repo you'll run `--auto` against repeatedly — it's read-only, cheap
  relative to a full plan/execute cycle, and improves Gate 1's grounding
  notes.
- Always plan with `--dry-run` first and read `IMPROVEMENTS.md` before a
  real run — it costs one Architect + Gate 1 pass and lets you catch a bad
  goal string or a misconfigured cluster before any code gets written.
- Run `--validate-plan` if meaningful time has passed between planning and
  execution, or if you changed the code by hand in between — it's cheap
  (one Gate 1 pass, no Architect call, no execution) and prevents Gate 2
  from burning attempts on a task whose premise no longer holds.
- Keep `--base` and the goal string identical across `--dry-run` retries
  after a Ctrl+C — the architect checkpoint is keyed on both, so changing
  either forces a full re-plan.
- Point `--validate-plan`'s `[gate1]` config at a stronger/slower model
  than your live `--auto` Gate 1 if you can afford it — it only runs once
  per plan, not once per Architect batch.

**Avoid:**
- Don't skip `--dry-run` for a first run on an unfamiliar repo/goal — a
  full `--auto` run with no plan review means Gate 2 will attempt tasks
  you never had a chance to veto.
- Don't assume Gate 1's evidence-checking makes false positives
  impossible — it closes the "confirmed with a fabricated citation" hole,
  not the "confirmed with a real but off-topic citation" one. Read
  `IMPROVEMENTS.md` / `IMPROVEMENTS-FALSE.md`, don't rubber-stamp them.
- Don't interrupt and immediately assume "it'll pick up where it left
  off" for the planning phase the way it does for execution — only the
  Architect stage has a checkpoint; Gate 1 does not (see
  [Resumability](#resumability)).
- Don't run `--validate-plan` expecting it to build or fix a plan — it
  only removes stale/false-positive tasks from an existing one; run
  `--auto ... --dry-run` again if you need a fresh plan.

---

## Collect artifact reference

*(unchanged from the original COLLECT-25 notes — kept here for reference.)*

### Example run

```bash
$ python main.py --collect --check
collect check: no manifest at .../.collect/collect_manifest.json — collect has never run

$ python main.py --collect
collect collect: built 11 file(s) in .../.collect

$ python main.py --collect          # run again, nothing changed
collect collect: already up to date — nothing to do

$ echo "# comment" >> tools/collect/model.py   # simulate an edit
$ python main.py --collect --check
collect check: stale — a tracked file changed since the last collect run

$ python main.py --collect --refresh
collect refresh: tree unchanged — recomputed derived artifacts only, wrote 11 file(s)

$ python main.py --collect --module tools/collect/model.py
collect module: patched tools/collect/model.py and refreshed 11 file(s)
```

### What gets written to `.collect/`

| File | Contents |
|---|---|
| `artifact.json` | raw structural data (1.1MB in this repo) |
| `collect_manifest.json` | file hashes + git sha, used for freshness checks |
| `MODULE_MAP.md` | per-file symbol tables (signature, private?, docstring) |
| `ARCHITECTURE.md` | derived overview |
| `CONFIG_MAP.md` | where each config key is read |
| `CONTRACTS.md` | cross-module invariants (hand-seeded or derived) |
| `GATES.md` | the pipeline's quality gates |
| `FAIL_OPEN_REGISTRY.md` | silently-swallowed exceptions found in the code |
| `RISK_INDEX.md` | per-module risk score (LOC, blast radius, unguarded access, etc.) |
| `TEST_MAP.md` | zero/thin test coverage per module |
| `GLOSSARY.md` | term definitions used across the other files |

Example, from `MODULE_MAP.md`:

```markdown
## `analyze_logs.py`
Imports: `argparse`, `collections`, `datetime`, ...
| symbol | signature | private | docstring |
| analyze_logs.py:_chapter_num | _chapter_num(...) | yes | Extract chapter number... |
```

### Configuring it (`agents.ini`, `[collect]` section)

```ini
[collect]
enabled         = true      # master switch
dir             = .collect  # output dir (relative to project root)
use_in_auto     = false     # wire artifact into /auto (Architect + Gate 1 grounding notes)
use_in_doc      = false
use_in_bughunt  = false
staleness       = warn      # warn | refresh | ignore, on stale reads
llm_summaries   = true      # false = purely structural, no Pass B LLM prose
```

### COLLECT-25 — Language dispatch + tree-sitter-java parser (Java 17+)

Implements the first task of Epic H (Java support) on top of the existing
`jan-auto-agent` `feature-collect` branch.

- `COLLECT-25-java-support.patch` — full unified diff, apply with
  `git apply COLLECT-25-java-support.patch` from the repo root (branch
  `feature-collect`), or `git am` if you want it as a commit.
- `tools/collect/lang.py` — extension → language dispatch
  (`detect_language`, `Language.PYTHON` / `Language.JAVA`).
- `tools/collect/java_parser.py` — `tree-sitter` + `tree-sitter-java`
  wrapper (`parse_java`), same "recorded, not raised" failure contract as
  the Python `ast.parse` path. Chosen over `javalang` specifically because
  the grammar needs to cover Java 17+ (records, sealed types,
  pattern-matching `switch`).
- `tools/collect/model.py` — `ModuleRecord` gets a `language: str =
  "python"` field (default keeps old artifacts/tests working unchanged).
- `tools/collect/scanner.py` — `scan_repo` now walks both `.py` and
  `.java` files via the dispatcher instead of hardcoding `.py`; adds
  `scan_java_module` (parses Java files into empty-but-valid
  `ModuleRecord`s — symbol/import/except extraction is COLLECT-26/27, not
  in scope here).
- `tests/test_collect_lang_dispatch.py`, `tests/test_collect_java_parser.py`
  — cover dispatch and parser behavior, including Java 17+ constructs and
  tree-sitter's error-tolerant parsing of broken files.
- `requirements.txt` — `tree-sitter>=0.23`, `tree-sitter-java>=0.23`
  (optional — `.java` files degrade to a recorded `parse_error` if not
  installed; nothing about the Python-only path changes).

Install the optional dependency:

```bash
pip install tree-sitter tree-sitter-java
```

Note: applying the patch also updates
`tests/fixtures/collect_mini_repo_golden.json` — adding the `language`
field to `ModuleRecord.to_dict()` changes the canonical JSON byte output
that COLLECT-3's determinism test checks against, so the golden fixture had
to be regenerated. This is expected and intentional, not a hidden change.

**Verified:** `pytest tests/ -k collect` → 360 passed; `pytest tests/`
(full suite) → 2730 passed.
