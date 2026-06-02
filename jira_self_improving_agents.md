# Self-Improving Agent Architecture — Jira Task Breakdown

---

## EPIC-1 · Metrics Collection Foundation

> Lay the groundwork for all future self-improvement. Without run data, nothing else can be built.

---

### STORY-1.1 · Define and write run metrics after every pipeline execution

**What to change**
- Create `tools/metrics_collector.py` (new file)
- Edit `main.py` → `Orchestrator.run_pipeline()`: call `MetricsCollector.record()` at the end of every run

**What `metrics_collector.py` does**
```
MetricsCollector
  record(run: RunRecord) → appends to metrics.json
  load_recent(n: int) → returns last N RunRecord entries
```

**`RunRecord` fields to capture**
| Field | Source |
|---|---|
| `timestamp` | `time.strftime()` |
| `intent` | `parsed.intent` |
| `prompt_version` | read from PromptStore (EPIC-2), default `"hardcoded"` |
| `iterations_used` | final `iteration` value |
| `validator_status` | last `validation.get("status")` |
| `validator_feedback` | last `validation.get("feedback", "")` |
| `improvement_json_ok` | `bool` — did `json.loads()` succeed in ImprovementAgent |
| `elapsed_seconds` | `total_elapsed` |

**Storage**: `metrics.json` in project root, append-only list of JSON objects. Auto-created on first run.

**What we achieve**
Every pipeline run now leaves a permanent record. No UI needed yet — you can `cat metrics.json` and immediately see patterns (e.g. validator always failing on iteration 1, JSON always breaking on `optimize` intent).

---

### STORY-1.2 · Expose failure-pattern summary for downstream use

**What to change**
- Add `MetricsCollector.summarize_failures(n: int) → dict` method

**What it returns**
```json
{
  "total_runs": 12,
  "avg_iterations": 2.4,
  "json_parse_failure_rate": 0.25,
  "common_feedback": [
    "Missing reference to config_loader",
    "Import logging not resolved"
  ],
  "worst_intent": "optimize"
}
```

Logic: load last `n` records, compute averages, extract most-repeated substrings from `validator_feedback` using simple word-frequency count (no LLM needed here).

**What we achieve**
`PromptOptimizer` (EPIC-3) can call this one method and get a structured briefing on what's going wrong, without touching raw JSON itself. This is the learning signal.

---

---

## EPIC-2 · Prompt Version Store

> Every agent's system prompt becomes a versioned, rollback-capable artifact instead of a hardcoded string.

---

### STORY-2.1 · Extract all hardcoded prompts out of agent files

**What to change**
- `tools/validator_agent.py` → move the large f-string prompt template into a named constant `VALIDATOR_PROMPT_HARDCODED` at module top
- `tools/improvement_agent.py` → same, constant `IMPROVEMENT_PROMPT_HARDCODED`
- `agents.ini` already has `[validator_agent] system = ...` and `[improvement_agent] system_improve = ...` — these become the **initial seed** values for PromptStore

**What we achieve**
Hardcoded prompts are now explicit, named constants. They are the final fallback that PromptStore can always return to. Nothing yet changes in runtime behavior — this is a safe refactor.

---

### STORY-2.2 · Build `PromptStore`

**What to create**
- `tools/prompt_store.py` (new file)

**Structure**
```
PromptStore
  get_current(agent_name: str) → str
  push(agent_name: str, new_prompt: str, score: float) → None
  rollback(agent_name: str) → bool   # returns False if already at hardcoded
  get_hardcoded(agent_name: str) → str
```

**Storage**: `prompts.json` in project root, structure:
```json
{
  "validator_agent": {
    "stack": [
      { "version": 1, "prompt": "...", "score": 0.72, "created_at": "..." },
      { "version": 2, "prompt": "...", "score": 0.85, "created_at": "..." }
    ],
    "current_version": 2
  }
}
```

**Rules**
- Stack depth capped at 3 versions (configurable in `agents.ini` under `[prompt_store] max_versions = 3`)
- If stack is empty, `get_current()` returns the hardcoded constant from the agent module
- `rollback()` pops the top entry; if stack becomes empty, next `get_current()` returns hardcoded
- `prompts.json` auto-created on first `push()`

**What we achieve**
Agents now have a switchable prompt source with full version history and a guaranteed safe fallback. The store is a standalone module — nothing breaks if it's not wired yet.

---

### STORY-2.3 · Wire `PromptStore` into `ValidatorAgent` and `ImprovementAgent`

**What to change**
- `Orchestrator.__init__()` → instantiate `PromptStore`, pass it to both agents
- `ValidatorAgent.__init__()` → accept `prompt_store: PromptStore` parameter; call `prompt_store.get_current("validator_agent")` at the top of `validate()` instead of using the inline f-string template
- `ImprovementAgent.__init__()` → same pattern for `"improvement_agent"`

**What we achieve**
Both agents now pull their prompt dynamically at call time. Swapping or rolling back a prompt takes effect on the next pipeline run with zero code change. First observable improvement: you can manually edit `prompts.json` and immediately test a different prompt.

---

---

## EPIC-3 · Prompt Optimizer

> When failure patterns accumulate, use the LLM to rewrite the offending prompt into a better candidate.

---

### STORY-3.1 · Build `PromptOptimizer`

**What to create**
- `tools/prompt_optimizer.py` (new file)

**Interface**
```
PromptOptimizer(model, base_url, api_key, timeout)
  generate_candidate(agent_name: str, current_prompt: str, failure_summary: dict) → str
```

**How it works**
Calls the local Jan LLM with a meta-prompt:

```
You are a prompt engineering agent. You will be given:
1. A current system prompt used by an AI agent
2. A summary of recent failures when using that prompt

Rewrite the prompt to fix the identified failure patterns.
Keep the same JSON output format requirements.
Return only the new prompt text, nothing else.

CURRENT PROMPT:
{current_prompt}

FAILURE SUMMARY:
{json.dumps(failure_summary)}
```

Returns the raw text response (the new candidate prompt). No JSON parsing needed.

**Config in `agents.ini`**
```ini
[prompt_optimizer]
trigger_after_failures = 5
min_runs_before_optimize = 3
```

**What we achieve**
The system can now generate a new prompt candidate from real failure data. This is the "learning" step — accumulated validator feedback gets converted into an improved instruction set.

---

### STORY-3.2 · Add trigger logic to `Orchestrator`

**What to change**
- `main.py` → at the end of `run_pipeline()`, after `MetricsCollector.record()`:

```python
summary = self.metrics_collector.summarize_failures(n=10)
if summary["total_runs"] >= MIN_RUNS and summary["avg_iterations"] > THRESHOLD:
    candidate = self.prompt_optimizer.generate_candidate(
        agent_name="validator_agent",
        current_prompt=self.prompt_store.get_current("validator_agent"),
        failure_summary=summary
    )
    # Hand off to PromptEvaluator (EPIC-4)
```

**Thresholds configurable in `agents.ini`**
```ini
[prompt_optimizer]
trigger_avg_iterations = 2.0   # optimize if avg > this
trigger_json_fail_rate = 0.3   # optimize if JSON fail rate > this
```

**What we achieve**
Optimization is now automatic but gated. It only fires when there is enough data and the data shows a real problem. No spurious rewrites on the first few runs.

---

---

## EPIC-4 · Prompt Evaluator & Promotion

> A candidate prompt must prove itself before replacing the current one. Bad candidates are silently discarded.

---

### STORY-4.1 · Build `PromptEvaluator`

**What to create**
- `tools/prompt_evaluator.py` (new file)

**Interface**
```
PromptEvaluator(prompt_store, metrics_collector)
  evaluate(agent_name: str, candidate_prompt: str) → EvalResult
```

**`EvalResult`**
```python
@dataclass
class EvalResult:
    promoted: bool
    reason: str         # human-readable, logged to console
    score: float        # 0.0–1.0
```

**Scoring logic** (no LLM call needed — pure arithmetic on MetricsLog)

Take the last 5 runs from `metrics.json` as a baseline. Re-run the same inputs against the candidate prompt in shadow mode (call `ValidatorAgent.validate()` directly with the candidate prompt injected temporarily). Compare:

| Signal | Weight |
|---|---|
| `avg_iterations` lower | 40% |
| `json_parse_ok` rate higher | 35% |
| `validator_status == approved` rate higher | 25% |

If candidate score > current score by at least 0.05: promote. Otherwise: discard.

**What we achieve**
Candidates must demonstrably improve at least one real metric before they replace the working prompt. The threshold (`0.05`) prevents thrashing on noise.

---

### STORY-4.2 · Wire evaluation result back to `PromptStore`

**What to change**
- `main.py` → after `PromptOptimizer.generate_candidate()`:

```python
result = self.prompt_evaluator.evaluate("validator_agent", candidate)
if result.promoted:
    self.prompt_store.push("validator_agent", candidate, result.score)
    print(f"✅ Prompt promoted (score {result.score:.2f}) — {result.reason}")
else:
    print(f"⚠️  Candidate discarded — {result.reason}")
```

**What we achieve**
Full closed loop: fail → optimize → evaluate → promote or discard → log. The rollback stack in `PromptStore` means the previous version is always one call away if the promoted version turns out worse in production.

---

### STORY-4.3 · Add manual rollback command to the shell

**What to change**
- `main.py` → `main()` loop: handle `/rollback <agent_name>` as a new shell command alongside `/exit` and `/new`

```python
if user_input.startswith("/rollback"):
    parts = user_input.split()
    agent = parts[1] if len(parts) > 1 else "validator_agent"
    ok = orchestrator.prompt_store.rollback(agent)
    print(f"↩️  Rolled back {agent}" if ok else f"Already at hardcoded fallback for {agent}")
    continue
```

**What we achieve**
If a promoted prompt causes regressions you notice while using the tool, you can rollback instantly without touching files. The session stays live.

---

---

## EPIC-5 · Integration, Config & Observability

> Tie everything together cleanly and make the system transparent during normal use.

---

### STORY-5.1 · Add `[prompt_store]` and `[prompt_optimizer]` sections to `agents.ini`

**What to change**
- `agents.ini` → add:

```ini
[prompt_store]
max_versions = 3
store_path = prompts.json

[prompt_optimizer]
enabled = true
trigger_avg_iterations = 2.0
trigger_json_fail_rate = 0.30
min_runs_before_optimize = 5
```

**What we achieve**
All tuning knobs live in one file. Disabling the optimizer entirely is one line (`enabled = false`). No code changes needed to adjust sensitivity.

---

### STORY-5.2 · Print prompt version in pipeline header

**What to change**
- `tools/formatter.py` → `OutputFormatter.render()`: add prompt version to the header line

Current output:
```
Source: tools/ui.py               Target: Spinner
```

New output:
```
Source: tools/ui.py               Target: Spinner    Prompt: v2
```

`parsed` already flows through to `render()` — add `prompt_version: str` as a new parameter from `Orchestrator`.

**What we achieve**
You can see at a glance which prompt version was active during a run, making it easy to correlate output quality with version history.

---

### STORY-5.3 · Add `/prompts` introspection command to the shell

**What to change**
- `main.py` → handle `/prompts` command: print a summary table of current version, stack depth, and last score for each agent

```
prompt> /prompts
validator_agent    v2  (score 0.87)  rollback: v1, hardcoded
improvement_agent  v1  (score 0.74)  rollback: hardcoded
```

**What we achieve**
Full visibility into the current state of the self-improvement system without opening any JSON file.

---

---

## Summary · Build Order & Dependencies

```
EPIC-1  ──────────────────────────────────────────── no dependencies
  STORY-1.1  Create MetricsCollector + wire into Orchestrator
  STORY-1.2  Add summarize_failures()

EPIC-2  ──────────────────── no dependencies
  STORY-2.1  Extract hardcoded prompts to constants       ← safe refactor, do first
  STORY-2.2  Build PromptStore
  STORY-2.3  Wire into agents                             ← needs 2.1 + 2.2

EPIC-3  ──────────────────── needs EPIC-1 + EPIC-2
  STORY-3.1  Build PromptOptimizer
  STORY-3.2  Add trigger logic to Orchestrator

EPIC-4  ──────────────────── needs EPIC-2 + EPIC-3
  STORY-4.1  Build PromptEvaluator
  STORY-4.2  Wire evaluation result → PromptStore
  STORY-4.3  Add /rollback shell command

EPIC-5  ──────────────────── needs everything above
  STORY-5.1  agents.ini sections
  STORY-5.2  Prompt version in output header
  STORY-5.3  /prompts shell command
```

**New files created**: `tools/metrics_collector.py`, `tools/prompt_store.py`, `tools/prompt_optimizer.py`, `tools/prompt_evaluator.py`, `metrics.json` (auto), `prompts.json` (auto)

**Existing files modified**: `main.py`, `tools/validator_agent.py`, `tools/improvement_agent.py`, `tools/formatter.py`, `agents.ini`

**Files not touched**: `tools/prompt_parser.py`, `tools/search_agent.py`, `tools/block_extractor.py`, `tools/file_reader.py`, `tools/ui.py`
