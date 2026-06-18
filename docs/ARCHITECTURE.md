# Agent Pipeline — Architecture & Usage Guide

> This document covers the full system: how it works, what every key parameter does,
> how to use **`--auto` creative mode**, how to set up the knowledge base, and how to
> clean up the agent working directory between runs.

---

## Table of Contents

1. [Overview](#overview)
2. [Quick Start](#quick-start)
3. [Directory Layout](#directory-layout)
4. [How the System Works](#how-the-system-works)
   - [Interactive Mode](#interactive-mode)
   - [Autonomous Mode (`--auto`)](#autonomous-mode---auto)
   - [FAQ Mode (`--faq`)](#faq-mode---faq)
5. [The `--auto` Pipeline in Detail](#the---auto-pipeline-in-detail)
   - [Phase 1 — PLAN](#phase-1--plan)
   - [Phase 2 — EXECUTE](#phase-2--execute)
   - [Review and Approval Gates](#review-and-approval-gates)
6. [Creative Mode (`task_mode = creative`)](#creative-mode-task_mode--creative)
   - [Setting Up a Creative Project](#setting-up-a-creative-project)
   - [Creative-Specific Parameters](#creative-specific-parameters)
7. [Configuration Reference (`agents.ini`)](#configuration-reference-agentsini)
8. [Prompt Parameters in agents.ini](#prompt-parameters-in-agentsini)
9. [The `.agent/` Directory — What It Contains and How to Clean It](#the-agent-directory--what-it-contains-and-how-to-clean-it)
10. [Runtime Files Created by the System](#runtime-files-created-by-the-system)
11. [Utility Scripts](#utility-scripts)
12. [Commands Reference (Interactive Shell)](#commands-reference-interactive-shell)

---

## Overview

This is a **multi-agent code (and creative-writing) improvement pipeline** that connects a local or remote LLM to your project files. It can operate in several modes:

| Mode | How to invoke | What it does |
|------|--------------|--------------|
| Interactive shell | `python main.py [base_dir]` | REPL: type queries, get answers, run agents |
| One-shot | `python main.py --once "query"` | Run one query and exit |
| Autonomous | `python main.py --auto "goal"` | Full self-directed review → plan → execute cycle |
| Dry-run | `python main.py --auto "goal" --dry-run` | Plan only — writes IMPROVEMENTS.md, no code changes |
| FAQ | `python main.py --faq "question"` | Look up answer in a knowledge folder and exit |

The system reads all its configuration from **`agents.ini`** (or whichever file you pass with `--config`).

---

## Quick Start

```bash
# Interactive shell pointed at your project
python main.py /path/to/your/project

# Autonomous improvement run (code mode, default)
python main.py --auto "fix all type errors and add docstrings" --base /path/to/project

# Dry-run: produce the plan only, write IMPROVEMENTS.md, no code changes
python main.py --auto "improve current code" --dry-run --base /path/to/project

# Creative writing mode (set task_mode = creative in agents.ini first)
python main.py --auto "write chapter 3" --base /path/to/story

# FAQ lookup
python main.py --faq "how do I reset my password?"

# FAQ with machine-readable JSON output (exit 0 = found, exit 1 = not found)
python main.py --faq "question" --json
```

---

## Directory Layout

```
project-root/
├── main.py                  # Entry point
├── agents.ini               # Main configuration (all parameters live here)
├── agents_4k.ini            # Variant: tuned for 4 096-token context window
├── agents_32k.ini           # Variant: tuned for 32K context
├── agents_128k.ini          # Variant: tuned for 128K context
├── analyze_logs.py          # Utility: human-readable analytics for trace logs
├── view_trace.py            # Utility: inspect a single trace_<run_id>.jsonl file
├── prompts.json             # Auto-managed: promoted prompt versions (created on first promotion)
├── metrics.json             # Auto-managed: per-run metrics history (created on first run)
├── IMPROVEMENTS.md          # Written by --auto plan phase
│
├── tools/                   # All agent modules
│   ├── auto/                # Autonomous mode sub-system
│   │   ├── controller.py    # Autonomous run orchestrator
│   │   ├── pipeline.py      # PLAN + EXECUTE orchestration
│   │   ├── architect.py     # Cluster reviewer → candidate tasks
│   │   ├── repo_ingest.py   # File walker + cluster builder
│   │   ├── gate1_filter.py  # False-positive filter (Gate 1)
│   │   ├── inner_loop.py    # Coder → Executor → Validator attempts (Gate 2)
│   │   ├── outer_loop.py    # Round loop per task
│   │   ├── coder.py         # Code/text generator agent
│   │   ├── executor.py      # Acceptance check runner
│   │   ├── state.py         # .agent/ I/O (plan, progress, log)
│   │   ├── git_manager.py   # Git wrapper (commit, identity)
│   │   └── ...              # Other supporting modules
│   └── ...                  # Interactive-mode agents
│
├── knowledge/               # FAQ knowledge base (create this yourself)
│   └── *.txt / *.md         # One topic per file; see FAQ section
│
└── .agent/                  # Created at runtime by --auto
    ├── plan.json
    ├── progress.json
    ├── run.log
    ├── trace_<run_id>.jsonl # Per-run event trace (one file per run)
    ├── tasks/
    │   └── AUTO-T1/         # Per-task artefacts
    │       └── feedback_round_1.md
    ├── tickets/             # Defect investigation tickets
    └── workspace/           # Executor sandbox
```

---

## How the System Works

### Interactive Mode

```
python main.py [base_dir]
```

The REPL accepts free-form queries or slash commands. A query without a file path goes straight to the LLM as a chat message (with rolling session history). A query that includes a file path runs the full **search → validate → improve** pipeline on the named symbol inside that file.

### Autonomous Mode (`--auto`)

```
python main.py --auto "GOAL" [--base DIR] [--config FILE] [--dry-run]
```

Runs a two-phase fully autonomous loop:

1. **PLAN** — Reads your project, clusters files, sends clusters to an Architect LLM, filters candidates through Gate 1, prioritises them into a backlog, writes `IMPROVEMENTS.md` and `plan.json`, and optionally commits with git.
2. **EXECUTE** — Iterates every task in the backlog: for each task, runs Coder → Executor → Validator in a round loop. Commits passing tasks automatically.

State is saved to `.agent/` after every task. A run interrupted mid-way can be resumed by re-running the same command — it picks up from the last unfinished task.

With `--dry-run`, the system stops after the PLAN phase. No code is written, no commits are made. `IMPROVEMENTS.md` and `plan.json` are produced so you can review the plan before committing to a full run.

### FAQ Mode (`--faq`)

```
python main.py --faq "QUESTION" [--base DIR] [--json]
```

Searches the `knowledge/` folder (path set by `[faq_agent] knowledge_dir`), finds relevant files, and answers the question in the language the question was asked in. Returns exit code `0` if an answer was found, `1` if not. Adding `--json` makes stdout a machine-readable JSON object `{"found": true/false, "answer": "..."}` and suppresses all other output — useful for automation.

---

## The `--auto` Pipeline in Detail

```
Repo files
    │
    ▼
[Step 1] repo_ingest — walk & cluster files by pattern
    │
    ▼
[Step 2] architect — LLM reviews each cluster, produces candidate tasks
    │
    ▼ (creative mode only, when validate_plan_creative = true)
[Step 2b] plan_validator — checks plan vs. goal facts, may request revision
    │
    ▼
[Step 3] Gate 1 filter — rejects hallucinated / ungrounded candidates
    │
    ▼
[Step 4] backlog_prioritiser — builds ordered task list, writes IMPROVEMENTS.md
    │
    ▼
[Step 5] plan_emitter — commits plan.json + IMPROVEMENTS.md via git
    │
    ▼
─────────── For each task (EXECUTE phase) ───────────
    │
    ▼
[Outer loop, round 1 .. max_rounds_per_task (default 10)]
    │
    ├── [Inner loop, attempt 1 .. max_attempts_per_task (default 5 / 8 creative)]
    │       │
    │       ├── Coder — generates / edits target file
    │       ├── Executor — runs acceptance_check shell command (objective Gate 2)
    │       └── Validator — LLM reviews the output (subjective Gate 2)
    │
    ├── Both halves pass → CommitOnSuccess (git commit)
    │                   └── Regression check on all prior DONE tasks
    └── Rounds exhausted → ExhaustionHandler → knowledge note + ticket
```

### Review and Approval Gates

The system has **three review / confirmation checkpoints per run**:

| Gate | Stage | What it checks | On failure |
|------|-------|----------------|------------|
| **Gate 1** | Plan phase, after Architect | Is the candidate task grounded in a real file and symbol? Does the cited path actually exist? | Candidate is dropped from the backlog entirely |
| **Gate 2 — Executor** | Execute phase, per attempt | Does the `acceptance_check` shell command exit with code 0? | Coder is called again with structured error output |
| **Gate 2 — Validator** | Execute phase, per attempt | LLM subjective review: is the output correct, complete, and consistent? | Coder is called again with validator's reason + hints |

Both halves of Gate 2 must pass for an attempt to succeed. The Executor runs first (cheap, objective); the Validator only runs if the Executor passes.

In code mode the Validator checks whether the function body is complete, all referenced names are resolvable, and nothing appears cut off. In creative mode it checks story continuity, language consistency, and non-duplication of previous chapters.

On top of these per-attempt gates, in creative mode there are two additional optional passes:

- **Plan validator** (Step 2b) — checks the architect's plan against the goal before Gate 1. Controlled by `validate_plan_creative` and `plan_max_revisions` in `[architect]`.
- **Fact validator** — verifies factual claims in each generated chapter against the established canon. Controlled by `fact_check_creative` and `max_fact_revisions` in `[validator_agent]`.

---

## Creative Mode (`task_mode = creative`)

Creative mode switches the entire pipeline to prose generation. The Coder writes chapters, the Validator checks story consistency, and canon is tracked across chapters to prevent drift.

### Setting Up a Creative Project

**1. Create a project folder and a seed file.**

The folder must exist and contain at least one file so the ingestor has something to read. Create the folder, then create a short file with a few sentences to establish language and style. The agent reads the prose — not the filename — to detect language and tone:

```bash
mkdir my_story
cd my_story

cat > chapter_01.txt << 'EOF'
The old lighthouse keeper had not spoken to another soul in seven years.
Every evening he climbed the iron stairs, lit the lamp, and watched the sea.
Tonight, a boat was approaching that should not exist.
It carried no lights and made no sound, yet it moved against the current.
EOF
```

> **Why is the seed file needed?**
> The Architect reads existing files to plan tasks. An empty folder produces zero candidates.
> A few sentences are enough — they establish the language (`creative_language = auto` detects it),
> the tone, and the initial facts that must remain consistent.

**2. Set `task_mode = creative` in agents.ini.**

```ini
[auto]
task_mode = creative
```

**3. Optionally choose an ini variant for your model's context window.**

Creative mode produces longer outputs than code mode. If your model supports it, use `agents_32k.ini` or `agents_128k.ini` as your starting configuration:

```bash
python main.py --auto "write chapters 2 through 5" \
  --base ./my_story \
  --config ./agents_32k.ini
```

**4. Run.**

The agent will read your seed file, plan chapter-writing tasks, write each chapter as a separate file, validate continuity and language after each one, and commit every passing chapter with git.

### Creative-Specific Parameters

All of these live in `agents.ini`. Key creative knobs:

| Parameter | Section | What it does |
|-----------|---------|--------------|
| `task_mode` | `[auto]` | Set to `creative` to enable this mode (`code`, `docs`, `creative` are valid) |
| `creative_language` | `[coder]` | `auto` = detect from existing text; or force e.g. `"Russian"`, `"English"` |
| `max_tokens_creative` | `[coder]` | Token budget for each chapter (default 2048) |
| `num_ctx_creative` | `[coder]` | Context window forwarded to Ollama for creative calls (default 8192) |
| `max_attempts_per_task_creative` | `[auto]` | Attempts per chapter before giving up (default 8) |
| `dup_reject_ratio` | `[coder]` | Chapter rejected if it is ≥ this similar to any previous one (0.92 = 92%). Set 0 to disable |
| `validate_plan_creative` | `[architect]` | Run plan-validator before Gate 1 to catch contradictions with the goal |
| `plan_max_revisions` | `[architect]` | Max plan-revision loops before accepting the plan as-is (default 1) |
| `fact_check_creative` | `[validator_agent]` | Run fact-checker against established canon after Gate 2 |
| `max_fact_revisions` | `[validator_agent]` | Max fact-fix retries per chapter (default 1) |
| `max_tasks_creative` | `[architect]` | Cap tasks per cluster; prevents small models from generating many overlapping tasks (default 1) |
| `creative_acceptance_default` | `[auto]` | When `true`, a task without an `acceptance_check` is treated as automatically passing |
| `canon_check_every` | `[auto]` | Run canon validator every N tasks (default 3) |
| `canon_max_claims` | `[auto]` | Max factual claims to ground per chapter (bounds LLM calls, default 12) |

---

## Configuration Reference (`agents.ini`)

All parameters below are read from `agents.ini` (or the file passed to `--config`). None need to be passed on the command line. Sections are read with `configparser`; values fall back to the defaults shown when the key is absent.

---

### [api] / [api\_local] / [api\_remote]

Controls which LLM endpoint is used. `[api] active` selects the active profile.

```ini
[api]
active     = local      # "local" → reads [api_local]; "remote" → reads [api_remote]
verify_ssl = true       # Set to false to skip TLS certificate checks (self-signed certs)

[api_local]
base_url   = http://localhost:11434   # LLM server URL
api_key    = ollama                   # API key ("jan" for Jan, any string for Ollama)
model      = llama3.1:8b              # Model name as known to the server
api_format = ollama                   # "ollama" or "openai"
num_ctx    = 4096                     # Context window size — forwarded to Ollama only
                                      # For openai format: set context length in the server UI

[api_remote]
base_url   = https://api.server.com
api_key    = <your-token-here>
model      = qwen2.5-coder:32b
api_format = ollama
num_ctx    = 16384
```

`api_format = openai` uses `/v1/chat/completions`. `api_format = ollama` uses `/api/chat`. The `num_ctx` key is forwarded as `options.num_ctx` to Ollama only — it is ignored for openai-format servers.

---

### [loop]

Controls the interactive / one-shot pipeline validation loop (not `--auto`).

```ini
[loop]
max_iterations  = 3     # Maximum search → validate rounds per query
timeout_seconds = 2400  # Hard wall-clock timeout for a single pipeline run (seconds)
```

---

### [chat]

Controls interactive shell behaviour.

```ini
[chat]
use_context  = true   # Include session history in LLM calls
new_chat_key = /new   # Command to reset session history
exit_key     = /exit  # Command to quit
```

---

### [auto]

Controls the autonomous (`--auto`) run.

```ini
[auto]
git_user                       = auto-agent            # git user.name for agent commits
git_email                      = auto-agent@localhost  # git user.email for agent commits
max_runtime_min                = 0                     # 0 = no cap; >0 = stop after N minutes
max_tasks_per_run              = 0                     # 0 = no cap; >0 = stop after N tasks
exec_timeout_sec               = 120                   # Per acceptance-check timeout (seconds)
rewrite_every_n_rounds         = 2                     # Trigger architect rewrite after N failed rounds
max_rewrites                   = 3                     # Max architect rewrite attempts per task
task_mode                      = code                  # "code", "docs", or "creative"
max_attempts_per_task          = 5                     # Inner-loop attempt cap (code / docs mode)
max_attempts_per_task_creative = 8                     # Inner-loop attempt cap (creative mode)
creative_acceptance_default    = true                  # Treat missing acceptance_check as pass (creative)
max_compression_passes         = 2                     # Summary memory compression passes
max_fidelity_rounds            = 2                     # Fidelity check rounds (creative)
canon_check_every              = 3                     # Run canon validator every N tasks (creative)
max_canon_revisions            = 1                     # Max canon-fix retries per chapter
canon_max_claims               = 12                    # Max factual claims to ground per chapter
```

> **`max_rounds_per_task`** is not in the default `agents.ini` but is a valid key read by
> `outer_loop.py` under `[auto]`. Default is **10**. Add it to cap how many outer rounds
> the agent attempts per task before treating it as exhausted:
>
> ```ini
> [auto]
> max_rounds_per_task = 5
> ```

---

### [architect]

Controls the Architect LLM agent that produces candidate tasks from each file cluster.

```ini
[architect]
temperature          = 0.2     # Lower = more deterministic task proposals
max_tokens           = 512     # Token budget for architect response (code mode)
max_tokens_creative  = 1024    # Token budget in creative mode
max_file_chars       = 1500    # Characters of each file sent to the architect per review call
max_files_per_review = 3       # Files per cluster review call
validate_plan_creative = true  # Check plan vs. goal before Gate 1 (creative mode only)
plan_max_revisions   = 1       # Max plan-revision loops before accepting as-is
max_tasks_creative   = 1       # Max tasks per cluster in creative mode
rewrite_max_tokens   = 256     # Tokens for architect rewrite-strategy response
rewrite_temperature  = 0.4     # Temperature for rewrite strategy call

# Optional: define your own file clusters (default is four built-in clusters)
# Format: name:glob1,glob2,...  — one definition per line or separated by semicolons
# The last entry acts as a catch-all. If absent, a "support:*" catch-all is added.
# clusters =
#     entry:main*,*cli*,*app*
#     agents:*agent*,tools/auto/*
#     io:*reader*,*formatter*,*parser*
#     support:*
```

---

### [gate1]

Controls Gate 1 — the fast false-positive filter that runs after the Architect.

```ini
[gate1]
temperature       = 0.0    # 0 = fully deterministic
max_tokens        = 128    # Just enough for {"present": bool, "reason": "..."}
max_context_lines = 25     # Lines of file context shown to Gate 1
max_block_chars   = 1200   # Max characters of the target block shown to Gate 1
```

---

### [coder]

Controls the Coder agent that generates or edits code / prose.

```ini
[coder]
temperature          = 0.2    # Lower = more conservative edits
max_tokens           = 800    # Token budget per generation (code mode)
max_tokens_creative  = 2048   # Token budget per chapter (creative mode)
max_file_chars       = 2000   # Max file characters sent to the coder
num_ctx_creative     = 8192   # Ollama context window override for creative calls
context_probe        = true   # Fetch referenced symbols before generation
max_chars_per_dep    = 400    # Characters of each dependency injected into prompt
max_dep_chars        = 1200   # Total dependency character budget
dup_reject_ratio     = 0.92   # Creative: reject chapter if ≥92% similar to any prior one (0 = off)
creative_language    = auto   # "auto" = detect from existing text; or "Russian", "English", etc.
```

---

### [inner\_loop] / [validator\_agent]

Controls the per-attempt validation cycle (subjective half of Gate 2).

```ini
[inner_loop]
temperature = 0.1    # Sampling temperature for the validator

[validator_agent]
temperature         = 0.1
max_tokens          = 200    # {"approved": bool, "feedback": "...", "hints": [...]}
max_hints           = 2      # Max hint items returned on rejection
fact_check_creative = true   # Run fact-checker against established canon (creative mode)
max_fact_revisions  = 1      # Max fact-fix retries per chapter
```

---

### [context\_broker]

Controls how many dependency symbols are injected into the Coder's context window.

```ini
[context_broker]
max_block_chars = 400   # Hard cap per resolved symbol (characters)
max_symbols     = 3     # Max symbols injected total (3 × 400 ≈ 400 tokens)
```

---

### [search]

Controls the file walker used by the SearchAgent and the Architect ingestor.

```ini
[search]
full_file_max_chars  = 3000    # Max characters for a full-file context probe
max_depth            = 8       # Directory traversal depth limit
max_file_kb          = 200     # Skip files larger than this (KB) in code mode
max_file_kb_creative = 400     # File size limit in creative mode (prose files are larger)
skip_dirs            = __pycache__,.venv,node_modules,.git,dist,build,venv,.tox
```

---

### [output]

Controls what the interactive / one-shot pipeline prints.

```ini
[output]
stream_agents        = true    # Stream tokens to stdout as they arrive (false = print after)
show_context_lines   = true    # Show surrounding context lines in results
show_timing          = true    # Print elapsed time at end of each run
show_iteration_count = true    # Print iteration number in result headers
```

---

### [prompt\_store] / [prompt\_optimizer]

Controls the auto-promotion of improved validator prompts discovered at runtime.

```ini
[prompt_store]
max_versions = 3              # Keep at most N historical prompt versions per agent
store_path   = prompts.json   # Where promoted prompts are persisted (relative to cwd)

[prompt_optimizer]
enabled                  = true   # Enable / disable prompt auto-tuning
temperature              = 0.4
trigger_avg_iterations   = 2.0    # Trigger optimiser when recent avg iterations > this
trigger_json_fail_rate   = 0.30   # Trigger when JSON parse failure rate > 30%
min_runs_before_optimize = 5      # Minimum recorded runs before optimiser activates
```

---

### [trace]

Controls the event trace written to `.agent/trace_<run_id>.jsonl` for `--auto` runs.

```ini
[trace]
enabled               = true    # Set to false to disable trace entirely
max_field_chars       = 4000    # Truncate large fields in the trace to this many chars
console_echo          = true    # Echo trace events to stdout while running
console_preview_chars = 300     # Characters of event payload previewed on console
```

> **Note:** The `path` key shown in older config examples has no effect. The trace file is
> always written to `.agent/trace_<run_id>.jsonl` — one file per run, inside the `.agent/`
> directory. The filename includes the run ID so multiple runs never overwrite each other.
> Use `analyze_logs.py` or `view_trace.py` to inspect them.

---

### [faq\_agent]

Controls the FAQ / knowledge-base resolver.

```ini
[faq_agent]
knowledge_dir        = ./knowledge     # Folder with .txt / .md knowledge files
extensions           = .txt,.md        # File extensions to load from that folder
temperature          = 0.0             # 0 = deterministic answers
max_tokens           = 1024
not_found_marker     = NOT FOUND       # Exact string returned when nothing matches
smart_search         = true            # Two-stage: keyword-extract then targeted lookup
keyword_max_tokens   = 64              # Tokens for keyword-extraction response
max_candidates       = 5              # Top-N files tried individually in Stage 1 smart search
validate_answer      = true           # Second LLM call validates the answer before returning
validate_temperature = 0.0
validate_max_tokens  = 64
revalidate_grounding = true           # Extra grounding pass to catch inverted / off-topic answers
```

---

### [direct\_chat]

Controls the free-form chat fallback (queries with no file path in the interactive shell).

```ini
[direct_chat]
temperature = 0.3
```

> **`history_max_turns`** is not in the default `agents.ini` but is a valid key read by the
> direct-chat handler, defaulting to **10**. Each turn = one user message + one assistant
> message; the oldest turn is dropped when the cap is reached. Add it to change the depth:
>
> ```ini
> [direct_chat]
> history_max_turns = 5
> ```

---

### [file\_editor]

Controls the `/edit` command (in-place file editing with backup + diff).

```ini
[file_editor]
temperature            = 0.2
max_tokens             = 512
prev_context_every     = 2     # Show the editor its own previous attempt for N consecutive
                                # retries, then one "clean" pass (feedback only), then repeat.
                                # 0 = disabled (never show previous attempt)
prev_context_max_chars = 2000  # Hard cap on injected previous-attempt text (chars)
```

---

### [improvement\_agent]

Controls the Improvement Agent (runs on `improve`, `fix`, `optimize`, `explain` intents).

```ini
[improvement_agent]
temperature = 0.4
max_tokens  = 1000
```

---

## Prompt Parameters in agents.ini

Several agents accept a **`system`** key (and mode-specific variants such as `system_creative`) that override the built-in system prompt for that agent. These keys are listed below by section and key name — the actual prompt text is intentionally omitted from this document.

| Section | Key | Agent / purpose |
|---------|-----|-----------------|
| `[architect]` | `system` | Architect review (code mode) |
| `[architect]` | `rewrite_system` | Architect rewrite strategy |
| `[validator_agent]` | `system` | Validator (code mode) |
| `[validator_agent]` | `system_creative` | Validator (creative mode) |
| `[faq_agent]` | `system` | FAQ resolver |
| `[faq_agent]` | `keyword_system` | Keyword extraction (smart search Stage 1) |
| `[faq_agent]` | `validate_system` | Answer validation pass |
| `[main_agent]` | `system_delegate` | Chief routing agent |
| `[main_agent]` | `system_assemble` | Context assembly agent |
| `[search_agent]` | `system` | Reference analyser |
| `[improvement_agent]` | `system_improve` | Code reviewer |
| `[improvement_agent]` | `system_explain` | Code explainer |

To override any prompt, write a replacement value after the key on a single line, using `\n` for line breaks. The mode-specific variants (`system_creative`, `system_docs`) take priority over the bare `system` key in their respective modes — a bare `system` override is only used in code mode.

---

## The `.agent/` Directory — What It Contains and How to Clean It

Every `--auto` run creates and maintains a `.agent/` directory inside your project's `base_dir`.

```
.agent/
├── plan.json                  # Task backlog — all tasks with status (todo/in_progress/done/blocked)
├── progress.json              # Run-level counters, status, and stop_reason
├── run.log                    # Append-only human-readable event log
├── trace_<run_id>.jsonl       # Per-run event trace (one file per autonomous run)
├── tasks/
│   ├── AUTO-T1/               # One folder per task
│   │   └── feedback_round_1.md   # Compact round feedback read by Coder in next round
│   └── AUTO-T2/
├── tickets/                   # Investigation tickets for exhausted tasks
│   └── ticket_AUTO-T3.md
├── workspace/                 # Executor sandbox for running acceptance checks
└── auto_prompts.json          # Written when auto-tuner promotes a validator prompt this run
```

### Resuming a Run

If you re-run the same `--auto` command while `.agent/` exists, the system detects it and **resumes from the last unfinished task**, skipping everything already marked `done`. You do not need to delete `.agent/` between resumed runs.

### Starting Fresh (Full Reset)

To start a completely new autonomous run on the same project:

```bash
# Remove the entire agent state directory
rm -rf .agent/

# Also remove plan and runtime files in the project root if desired
rm -f IMPROVEMENTS.md prompts.json metrics.json
```

After deletion, the next `--auto` invocation treats the project as brand-new and runs the full PLAN phase from scratch.

### Partial Cleanup — Keep History, Reset Pending Tasks

To keep the log but rebuild the plan from scratch on the next run, delete only `plan.json`:

```bash
rm -f .agent/plan.json
```

The next run will rebuild the plan; the log, trace files, and ticket history remain.

---

## Runtime Files Created by the System

| File | Location | Created by | Purpose |
|------|----------|-----------|---------|
| `.agent/` | `base_dir/` | `--auto` first run | All persistent state for autonomous runs |
| `.agent/trace_<run_id>.jsonl` | `.agent/` | `--auto` each run | Per-run event trace; one file per run |
| `IMPROVEMENTS.md` | `base_dir/` | PLAN phase | Human-readable task list; committed to git |
| `prompts.json` | cwd | Prompt optimizer | Promoted validator prompt versions |
| `metrics.json` | cwd | Metrics collector | Per-run metrics (iterations, timing, failures) |
| `<file>.bak` | beside edited file | `/edit` command | Backup before in-place edit |

> **Interactive / one-shot mode** does not write a trace file. Tracing is only active during
> `--auto` runs, where each run produces its own `.agent/trace_<run_id>.jsonl`.

---

## Utility Scripts

Two scripts in the project root help you inspect what the agent did.

### `analyze_logs.py` — Run analytics

```bash
# Show summary for the newest trace in .agent/
python analyze_logs.py .agent/

# Show summary for a specific trace file
python analyze_logs.py .agent/trace_abc123def456.jsonl

# Show all runs in the directory
python analyze_logs.py .agent/ --all-runs

# Include the prompt rewrite report
python analyze_logs.py .agent/ --rewrites

# Rewrite report only
python analyze_logs.py .agent/ --rewrites-only
```

Shows: total tasks, iterations, approve/reject counts, prompt changes, per-task breakdown, and a human-readable timeline.

### `view_trace.py` — Inspect a single trace

```bash
python view_trace.py .agent/trace_<run_id>.jsonl [options]
```

Renders individual trace events from a single run. Useful for debugging a specific run without the aggregated summary view.

---

## Commands Reference (Interactive Shell)

| Command | Description |
|---------|-------------|
| `/help` or `/?` | Show the help text |
| `/auto <goal>` | Launch autonomous mode for `<goal>` from inside the REPL |
| `/faq <question>` | Look up a question in the knowledge base |
| `/faq --list` | List all files currently loaded in the knowledge base |
| `/search <q> in <f>` | Answer a question using the whole file (no block extraction) |
| `/edit <instr> in <f>` | Apply an instruction to a file and write it back (validated; saves `.bak`; shows diff) |
| `/prompts` | Show active prompt version + rollback chain for each agent |
| `/rollback [agent]` | Roll back one prompt version (default: `validator_agent`) |
| `/reload` | Re-read `agents.ini` and rebuild all agents without restarting |
| `/new` | Reset session history |
| `/exit` | Quit |

### Work Query Syntax (Interactive)

```
<action> <symbol> in <file>      e.g.  improve handle_request in app.py
show <file>                       e.g.  show app.py
```

**Actions:** `show` / `view` / `get` → display; `improve` / `fix` / `optimize` → review + suggest code; `explain` / `describe` → explanation only.

A query with no file path is sent directly to the LLM as a chat message with rolling session history (depth controlled by `[direct_chat] history_max_turns`, default 10).

---

## Full Example — Autonomous Creative Run

```bash
# 1. Create the project folder
mkdir lighthouse_story
cd lighthouse_story

# 2. Create the seed file (a short opening — establishes language and tone)
cat > chapter_01.txt << 'EOF'
The old lighthouse keeper had not spoken to another soul in seven years.
Every evening he climbed the iron stairs, lit the lamp, and watched the sea.
Tonight, a boat was approaching that should not exist.
It carried no lights and made no sound, yet it moved against the current.
EOF

# 3. In agents.ini, set:
#   [auto]    task_mode = creative
#   [coder]   max_tokens_creative = 2048
#   [coder]   num_ctx_creative = 32768   (match your model's window)
#   [coder]   creative_language = English

# 4. Run a dry-run first to review the plan before committing
python main.py \
  --auto "Write chapters 2, 3, and 4 continuing the story" \
  --base ./lighthouse_story \
  --dry-run

# Review the plan:
cat IMPROVEMENTS.md

# 5. If the plan looks good, run for real
python main.py \
  --auto "Write chapters 2, 3, and 4 continuing the story" \
  --base ./lighthouse_story

# 6. Check what happened
python analyze_logs.py .agent/

# 7. To start over (reset agent state, keep your own files)
rm -rf .agent/ IMPROVEMENTS.md prompts.json metrics.json
```

---

*Generated from source analysis of `main.py`, `tools/auto/controller.py`, `tools/auto/pipeline.py`,
`tools/auto/outer_loop.py`, `tools/auto/inner_loop.py`, `tools/auto/run_trace.py`,
`tools/agent_trace.py`, and `agents.ini`.*
