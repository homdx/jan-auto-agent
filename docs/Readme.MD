# Agent Pipeline — Documentation

## Table of Contents

1. [Overview](#1-overview)
2. [Quick Start](#2-quick-start)
3. [Running Modes](#3-running-modes)
4. [Task Modes](#4-task-modes)
5. [Configuring Prompts per Task Mode](#5-configuring-prompts-per-task-mode)
6. [agents.ini Reference](#6-agentsini-reference)
7. [Autonomous Pipeline — Architecture](#7-autonomous-pipeline--architecture)
8. [Validation Graph (Gate 1 + Gate 2)](#8-validation-graph-gate-1--gate-2)
9. [Context Broker — Pull-Model Context](#9-context-broker--pull-model-context)
10. [Architect Rewrite Policy (LOOP-2)](#10-architect-rewrite-policy-loop-2)
11. [Bug Fix Loop (AUTO-D2)](#11-bug-fix-loop-auto-d2)
12. [Ticket Store (AUTO-D1)](#12-ticket-store-auto-d1)
13. [Prompt Optimizer & Auto-Tuner](#13-prompt-optimizer--auto-tuner)
14. [Trace & Analytics](#14-trace--analytics)
15. [CLI Commands](#15-cli-commands)

---

## 1. Overview

A local-LLM agent pipeline for code review, documentation improvement, and creative writing. It wraps one or more Ollama / OpenAI-compatible endpoints and orchestrates a multi-agent loop:

```
Repo Ingest → Architect (checkpoint) → Gate 1 (false-positive filter)
           → Backlog Prioritiser → Plan
           → Outer Loop  (rounds)
               └── Inner Loop  (attempts per round)
                       ├── Coder          (emits context_request)
                       ├── ContextBroker  (resolves requested symbols via AST)
                       ├── Executor       (acceptance check — Gate 2a)
                       └── Validator      (LLM completeness — Gate 2b)
                               PASS → CommitOnSuccess
                               FAIL → structured feedback + prefetched context → next attempt
           → Exhaustion Handler → knowledge.md + TicketStore
           → BugFixLoop  (handles regressions detected by later tasks)
```

All configuration lives in `agents.ini` at the project root. No code changes are needed to switch model, mode, or prompt.

---

## 2. Quick Start

```bash
# Interactive shell (uses cwd as project root)
python main.py

# Interactive shell with explicit project root
python main.py /home/user/myproject

# One-shot query and exit
python main.py --once "improve load_config in tools/actions.py"

# Autonomous improvement run
python main.py --auto "improve error handling across the codebase" --base /home/user/myproject

# Dry-run (plan only — no execution, no commits)
python main.py --auto "improve error handling" --dry-run --base /home/user/myproject
```

---

## 3. Running Modes

### Interactive mode

```bash
python main.py [base_dir]
```

Starts a `prompt>` REPL. Type `/help` to list commands. Any query that doesn't start with `/` is routed through the pipeline automatically.

### One-shot mode

```bash
python main.py --once "QUERY" [--base DIR] [--config FILE]
```

Runs a single query, prints the result, and exits with code `0` (success) or `1` (error). Useful for CI integration.

### Autonomous mode

```bash
python main.py --auto "GOAL" [--base DIR] [--config FILE]
```

Launches the full autonomous improvement pipeline:

1. Ingests the repo and clusters files.
2. The Architect reviews each cluster and generates candidate tasks — checkpoint saved to `.agent/architect_checkpoint.json`.
3. Gate 1 filters false positives across three stages.
4. The Backlog Prioritiser ranks tasks and emits `IMPROVEMENTS.md` + `plan.json`.
5. The Outer/Inner loop executes each task, validates it, and commits on success.
6. Exhausted tasks produce a `knowledge.md` note and an investigation ticket in `.agent/tickets/`.

State is persisted under `.agent/` — kill the process and restart to resume from the checkpoint.

Also available from the interactive shell: `/auto improve error handling across the codebase`

### Dry-run mode

```bash
python main.py --auto "GOAL" --dry-run --base DIR
```

Runs only the plan phase (ingest → architect → gate1 → prioritise) and writes `IMPROVEMENTS.md` and `plan.json` without executing tasks or making git commits. Use this to preview what the agent would do before committing to a run.

---

## 4. Task Modes

The `task_mode` key in `agents.ini [auto]` controls which domain the Architect and Gate 1 agents operate in. It applies only to autonomous mode — interactive and one-shot queries always use the code-oriented prompts built into each agent.

| `task_mode` | Architect reviews… | Gate 1 checks… | Validator framing |
|---|---|---|---|
| `code` (default) | Code quality, logic, security | Code problem present? | Code completeness validator |
| `docs` | Missing sections, outdated commands, unclear prose | Documentation problem present? | Documentation change validator |
| `creative` | Weak imagery, pacing, tone, structure | Creative problem present? | Creative writing editor |

### Code mode (default)

No configuration change needed. The built-in prompts target Python / JS / TS / Go and similar source files. The validator checks that all referenced symbols are resolved before approving.

### Docs mode

```ini
[auto]
task_mode = docs
```

The Architect identifies documentation problems (missing sections, broken examples, outdated commands) and cites the exact file path and line range. The validator checks whether the prose improvement is complete and accurate.

### Creative mode

```ini
[auto]
task_mode = creative
```

The Architect flags weak imagery, inconsistent tone, pacing issues, and structural problems. The validator checks whether the revision is complete and faithful to the task.

---

## 5. Configuring Prompts per Task Mode

Each agent supports a mode-specific system prompt defined in `agents.ini`. Priority order (highest → lowest):

1. `system` — overrides all modes.
2. `system_<task_mode>` — applies only when that mode is active.
3. Built-in hardcoded prompt — fallback when neither key is present.

### Full example

```ini
[auto]
task_mode = docs

[architect]
system_docs = You are a senior technical writer performing a documentation review.
    Identify missing sections, outdated commands, unclear explanations, and broken
    examples. Cite the exact file path and line range where each issue lives.
    symbol must be null. acceptance_check must be a shell command such as
    'grep -q "expected heading" file.md' or 'true' if not checkable.
    Return only the JSON array of candidates.

[gate1]
system_docs = You are a documentation reviewer performing a false-positive check.
    You will be shown a prose excerpt and a description of a claimed documentation
    problem. Decide whether the problem is genuinely present in the shown text.
    Return only JSON: {"present": true|false, "reason": "<one sentence>"}.

[validator_agent]
system = You are a documentation change validator. Given a task description and
    the revised file content, decide whether the documentation improvement is
    complete and accurate. Return ONLY a JSON object:
    {"approved": true|false, "feedback": "<one sentence>", "hints": ["<specific hint>"],
    "suggested_approach": "<optional>"}.
    Each hint MUST point to a specific section, heading, or line.
    Omit hints when approved=true.
```

> **Tip:** `[validator_agent] system` (without a mode suffix) overrides all modes. For per-mode validator behaviour, use `system_code`, `system_docs`, `system_creative` and leave `system` absent.

---

## 6. agents.ini Reference

### `[api]`

| Key | Default | Description |
|---|---|---|
| `active` | `local` | Which API profile to use: `local` or `remote` |
| `verify_ssl` | `true` | Set `false` to skip SSL cert verification (self-signed certs) |

### `[api_local]` / `[api_remote]`

| Key | Default | Description |
|---|---|---|
| `base_url` | `http://localhost:1337/v1` | API endpoint URL |
| `api_key` | `jan` | Bearer token |
| `model` | `qwen3:8b` | Model name |
| `api_format` | `openai` | `openai` or `ollama` |
| `num_ctx` | `0` | Context window size (Ollama only; `0` = server default) |

### `[loop]`

| Key | Default | Description |
|---|---|---|
| `max_iterations` | `3` | Validator retry cap for interactive/one-shot mode |
| `timeout_seconds` | `4800` | Wall-clock timeout per pipeline run |

### `[auto]`

| Key | Default | Description |
|---|---|---|
| `task_mode` | `code` | Domain mode: `code` \| `docs` \| `creative` |
| `git_user` | `auto-agent` | Git author name for autonomous commits |
| `git_email` | `auto-agent@localhost` | Git author email |
| `max_runtime_min` | `0` | Wall-clock cap in minutes (`0` = disabled) |
| `max_tasks_per_run` | `0` | Task cap per session (`0` = disabled) |
| `exec_timeout_sec` | `600` | Timeout for acceptance-check scripts |
| `llm_timeout_sec` | `600` | Timeout for LLM API calls |
| `max_rounds_per_task` | `10` | Outer-loop round cap per task |
| `max_attempts_per_task` | `5` | Inner-loop attempt cap per round |
| `rewrite_every_n_rounds` | `2` | Failed rounds before the Architect rewrites the task |
| `max_rewrites` | `5` | Hard cap on Architect rewrites per task lifetime (`0` = disabled) |

### `[architect]`

| Key | Default | Description |
|---|---|---|
| `temperature` | `0.2` | Sampling temperature |
| `max_tokens` | `2048` | Token budget per call |
| `max_file_chars` | `4000` | Max characters of a single file in the review prompt |
| `max_files_per_review` | `6` | Max files per LLM call (larger clusters are batched) |
| `system` | *(built-in)* | Override system prompt for all modes |
| `system_docs` | *(built-in)* | Override system prompt for docs mode |
| `system_creative` | *(built-in)* | Override system prompt for creative mode |
| `rewrite_max_tokens` | `512` | Token budget for the TaskRewriter call |
| `rewrite_temperature` | `0.4` | Temperature for the TaskRewriter call |
| `rewrite_system` | *(built-in)* | System prompt for the TaskRewriter |

### `[gate1]`

| Key | Default | Description |
|---|---|---|
| `temperature` | `0.0` | Deterministic by default |
| `max_tokens` | `256` | Token budget for the presence-check call |
| `max_context_lines` | `60` | Max source lines included in the prompt |
| `max_block_chars` | `4000` | Max characters of a code block sent to the LLM |
| `system` | *(built-in)* | Override system prompt for all modes |
| `system_docs` | *(built-in)* | Override for docs mode |

### `[validator_agent]`

| Key | Default | Description |
|---|---|---|
| `temperature` | `0.1` | Sampling temperature |
| `system` | *(built-in)* | Override for all modes (highest priority) |
| `max_hints` | `3` | Max actionable hints returned per rejection |

### `[coder]`

| Key | Default | Description |
|---|---|---|
| `temperature` | `0.2` | Sampling temperature |
| `max_tokens` | `16384` | Token budget |
| `max_file_chars` | `8000` | Max characters of each target file in the coder prompt |

### `[inner_loop]`

| Key | Default | Description |
|---|---|---|
| `temperature` | `0.1` | Temperature for the autonomous Gate 2 validator |

### `[prompt_optimizer]`

| Key | Default | Description |
|---|---|---|
| `enabled` | `true` | Enable / disable the auto-optimizer |
| `temperature` | `0.4` | Optimizer temperature |
| `trigger_avg_iterations` | `2.0` | Trigger when average iterations exceed this |
| `trigger_json_fail_rate` | `0.30` | Trigger when JSON parse failure rate exceeds this |
| `min_runs_before_optimize` | `5` | Minimum runs before optimization is considered |

### `[trace]`

| Key | Default | Description |
|---|---|---|
| `enabled` | `true` | Enable agent trace output |
| `max_field_chars` | `4000` | Truncation limit per field |
| `console_echo` | `true` | Print trace events to console |
| `console_preview_chars` | `600` | Characters of prompt/response to preview live (`0` = header only) |

> **Note:** The trace file path is not configurable. Each run automatically creates `.agent/trace_<run_id>.jsonl`.

---

## 7. Autonomous Pipeline — Architecture

![Architecture diagram](Architecture.png)

The pipeline runs in two phases.

### Plan phase

```
RepoIngestor
    └─ walks base_dir, clusters files by fnmatch pattern ([architect] clusters)
Architect
    └─ one LLM call per cluster → list[CandidateTask]
    └─ checkpoint: .agent/architect_checkpoint.json  (resume-safe)
Gate 1 Filter
    ├─ Stage A (no LLM): file exists · symbol resolves · line range in bounds
    ├─ Stage B (LLM):    is the problem actually present? fail-closed on bad JSON
    └─ Stage C (no LLM): deduplicate on (file, symbol/line, title)
BacklogPrioritiser
    └─ scores and ranks accepted candidates
PlanEmitter
    └─ writes plan.json + IMPROVEMENTS.md · commits plan to git
```

### Execute phase

For each task in the backlog:

```
OuterLoop  (rounds, default max 10)
    └─ InnerLoop  (attempts per round, default max 5)
           ├─ Coder          → writes / patches target file(s)
           │                   may emit context_request: ["Symbol1", "Symbol2"]
           ├─ ContextBroker  → resolves context_request symbols from AST index
           │                   injects PREFETCHED CONTEXT into next attempt prompt
           ├─ Executor       → runs acceptance_check shell command  (Gate 2a)
           └─ Validator      → LLM confirms the change is complete  (Gate 2b)
                   PASS → CommitOnSuccess  (git add -u && git commit)
                   FAIL → structured feedback (LOOP-1) → next attempt
    ROUND EXHAUSTED → compact feedback_round_N.md · fresh context next round
    REWRITE TRIGGER → Architect rewrites task with new strategy  (LOOP-2)
    TASK EXHAUSTED  → ExhaustionHandler → knowledge.md + investigation ticket
    REGRESSION      → BugFixLoop → synthesise fix task → re-run C-loop  (AUTO-D2)
```

State is stored under `.agent/`:

```
.agent/
├── plan.json                    task backlog
├── progress.json                run counters, status, stop reason
├── run.log                      append-only human-readable log
├── architect_checkpoint.json    partial architect results (resume-safe)
├── tasks/<id>/
│   ├── feedback_round_*.md
│   └── knowledge.md             (exhausted tasks only)
└── tickets/                     investigation and bug tickets (AUTO-D1)
    └── <ticket-id>.json
```

---

## 8. Validation Graph (Gate 1 + Gate 2)

![Review cycle](Review_cycle.png)

### Gate 1 — false-positive filter (plan phase)

Runs before any code is written. Rejects candidates where the described problem does not actually exist in the cited code.

```
CandidateTask
    │
    ├─ Stage A  (no LLM)
    │   ├── cited file exists?            → reject if not
    │   ├── cited symbol resolves?        → reject if not
    │   └── cited line range in bounds?   → reject if not
    │
    ├─ Stage B  (one LLM call)
    │   └── "is this problem present?" → {"present": true|false}
    │       fail-closed: unparseable response = rejected
    │
    └─ Stage C  (no LLM)
        └── deduplicate on (file, symbol/line, title)
```

Configure Gate 1 prompts in `agents.ini`:

```ini
[gate1]
system      = Your custom Gate 1 prompt for all modes.
system_docs = Your custom Gate 1 prompt for docs mode only.
```

### Gate 2 — inner-loop validator (execute phase)

Runs after the Coder writes code and the Executor runs the acceptance check. Both halves must pass before the task is committed.

```
Attempt N
    ├─ Executor (objective)   → runs acceptance_check → exit 0 = pass  (Gate 2a)
    └─ Validator (subjective) → LLM checks completeness               (Gate 2b)
            APPROVED → commit
            REJECTED → structured feedback (reason + up to max_hints hints)
                           + ContextBroker resolves any context_request symbols
                           → next attempt (same round)
```

Validator JSON response format (autonomous mode):

```json
{
  "approved": true,
  "feedback": "one sentence",
  "hints": ["specific hint pointing to file/line"],
  "suggested_approach": "optional"
}
```

Configure the validator in `agents.ini`:

```ini
[validator_agent]
temperature = 0.1
max_hints   = 3
system = You are a code completeness validator. Check: 1) Is the function body
    complete? 2) Are all called names in imports or related_code? 3) Any missing
    definitions? Return ONLY JSON: {"approved": true|false, "feedback": "...",
    "hints": ["..."], "suggested_approach": "..."}.
```

---

## 9. Context Broker — Pull-Model Context

The ContextBroker (`tools/auto/context_broker.py`) implements a pull model for context resolution.

In the old push model the Coder received a fixed char-budget slice of the codebase with no way to ask for more. The pull model lets the Coder declare what it needs:

```json
{
  "files": ["tools/auto/coder.py"],
  "context_request": ["Config", "_resolve_path"]
}
```

When a `context_request` is present the ContextBroker:

1. Searches `target_files` first (cheapest, most likely hit).
2. Falls back to a depth-first scan of all project Python files under `base_dir`.
3. Returns resolved source blocks as a `PREFETCHED CONTEXT` section injected into the next attempt's prompt.

Symbols that cannot be resolved are silently omitted — a missing symbol is not an error.

| Parameter | Default | Description |
|---|---|---|
| `max_block_chars` | `3000` | Hard cap per resolved block |
| `max_symbols` | `8` | Max symbols resolved per call |

---

## 10. Architect Rewrite Policy (LOOP-2)

When a task exhausts its per-round attempts repeatedly, the Architect rewrites the task description with a genuinely different strategy rather than retrying the same approach.

Trigger condition (with `rewrite_every_n_rounds = 2`): first rewrite fires at round 3, then every 2 rounds after that.

```
rnd >= 3  AND  (rnd - 1) % rewrite_every_n_rounds == 0
```

Each rewrite increments `impl_version` in the task state. The Coder always sees the current `impl_version` as a header in its prompt (LOOP-4), so it knows which strategy it is implementing.

Configure rewrite behaviour:

```ini
[auto]
rewrite_every_n_rounds = 2    # cadence after round 3
max_rewrites           = 5    # 0 = disable rewrites entirely

[architect]
rewrite_max_tokens    = 512
rewrite_temperature   = 0.4
rewrite_system        = You are a senior architect. Given a failing task and
    its history, produce a revised task description with a different strategy.
    Return only JSON.
```

---

## 11. Bug Fix Loop (AUTO-D2)

When a later task's acceptance check reveals a regression in already-committed work, the BugFixLoop (`tools/auto/bug_fix_loop.py`) handles recovery:

1. Opens a `BUG-<triggering_task_id>` ticket in the TicketStore.
2. Synthesises a fix task from the regression evidence.
3. Runs the fix task through the full C-loop (OuterLoop → InnerLoop).
4. If the fix passes, commits it and closes the ticket.
5. If the fix is exhausted, marks the ticket `"deferred"` and lets the run continue.

Ticket IDs are idempotent — if a bug ticket already exists for the same triggering task it is reused rather than duplicated.

```
Bug ticket:  BUG-<triggering_task_id>
Fix task id: BUG-FIX-<triggering_task_id>
```

---

## 12. Ticket Store (AUTO-D1)

All investigation and bug tickets are persisted as JSON files under `.agent/tickets/`.

| Field | Type | Description |
|---|---|---|
| `id` | str | Unique identifier, e.g. `"TICKET-AUTO-T1"` |
| `type` | str | `"bug"` \| `"investigation"` |
| `status` | str | `"open"` \| `"in-progress"` \| `"fixed"` \| `"deferred"` |
| `linked_task` | str | Task id this ticket concerns |
| `title` | str | Short human-readable description |
| `body` | str | Full detail / knowledge text |
| `created_at` | str | ISO-8601 UTC timestamp (immutable) |
| `updated_at` | str | ISO-8601 UTC timestamp (updated on every write) |

Tickets survive process restarts — they are plain JSON files on disk. The ExhaustionHandler creates `"investigation"` tickets; the BugFixLoop creates `"bug"` tickets.

---

## 13. Prompt Optimizer & Auto-Tuner

The pipeline automatically evolves the `validator_agent` prompt when failure signals are strong enough.

Trigger conditions (all must be true):

- `prompt_optimizer.enabled = true`
- At least `min_runs_before_optimize` runs recorded
- Either `avg_iterations > trigger_avg_iterations` or `json_parse_failure_rate > trigger_json_fail_rate`

Flow:

```
MetricsCollector → PromptOptimizer.generate_candidate()
               → PromptEvaluator.evaluate()
                       PASS threshold → PromptStore.push() → reload_agents()
                       FAIL           → candidate discarded
```

Manual prompt management (interactive shell):

```
/prompts              Show active version + rollback chain for each agent
/rollback             Roll back validator_agent one version
/rollback architect   Roll back a specific agent
/reload               Re-read agents.ini and rebuild all agents without restarting
```

Prompt versions are stored in `prompts.json` (up to `prompt_store.max_versions` per agent).

---

## 14. Trace & Analytics

All agent exchanges are written to `.agent/trace_<run_id>.jsonl` (one file per run, auto-named). Console preview is controlled by `console_preview_chars`.

View a trace:

```bash
python view_trace.py .agent/trace_<run_id>.jsonl
python view_trace.py .agent/trace_<run_id>.jsonl --sources architect
python view_trace.py .agent/trace_<run_id>.jsonl --summary
```

Analyze logs:

```bash
python analyze_logs.py .agent/                  # newest trace in directory
python analyze_logs.py .agent/ --all-runs       # all runs
python analyze_logs.py .agent/trace_abc.jsonl --run-id abc123
```

Shows: task summary, per-task iteration counts, approve/reject breakdown, prompt change history, event timeline.

---

## 15. CLI Commands

### Shell commands (interactive mode)

| Command | Description |
|---|---|
| `/help` or `/?` | Show command list |
| `/auto <goal>` | Launch autonomous mode with the given goal |
| `/search <q> in <f>` | Answer a question from the whole file |
| `/edit <instr> in <f>` | Apply an instruction to a file, validate, and write back (saves `.bak`, prints diff) |
| `/prompts` | Show active prompt version and rollback chain |
| `/rollback [agent]` | Roll back one prompt version (default: `validator_agent`) |
| `/reload` | Re-read `agents.ini` and rebuild all agents without restarting |
| `/new` | Reset the session |
| `/exit` | Quit |

### Pipeline queries (interactive or `--once`)

```
<action> <symbol> in <file>
```

| Action | What happens |
|---|---|
| `show` / `view` / `get` | Display the target block + imports |
| `improve` / `fix` / `optimize` | Review and suggest improved code |
| `explain` / `describe` | Explanation only, no code changes |

Examples:

```
improve load_config in tools/actions.py
show OuterLoop in tools/auto/outer_loop.py
explain validate in tools/validator_agent.py
/search how does gate1 work in tools/auto/gate1_filter.py
/edit fix grammar in docs/Readme.MD
```

A query with no file path is sent directly to the model as a conversational message.
