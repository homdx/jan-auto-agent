# COLLECT-24 — collect artifact wired into `--auto`

Branch: `feature/validate-plan` (commit `891d6af`).

## Problem this fixes

`--collect` built a structural artifact (`.collect/`) but `--auto` never
read it — `Controller.collect_context_for()` existed (COLLECT-23) and was
correct, but nothing ever called it. Running `--collect` before `--auto`
had zero effect on the code the LLM actually saw.

## What changed

### New: `tools/auto/collect_bridge.py`
`CollectBridge` — the single integration point:

* **Staleness policy (simplified per product decision):** only `status ==
  "fresh"` is ever used. `stale` is treated exactly like `absent` — no LLM
  check of "is this still accurate", just a plain fallback to standard
  `--auto` for the affected file/function. `[collect] staleness` in
  `agents.ini` still controls whether `loader.load()` itself rebuilds
  (`refresh`) or just warns (`warn`, default) — this module never
  triggers a rebuild, it only decides whether to USE what `load()` returned.
* **Static per-task context** — `context_for(file)` / `context_for_many(files)`:
  the COLLECT-23 block, budget-aware (`[collect] max_context_chars_auto`,
  default 1200 chars). Over budget → one LLM shrink call (reusing the Pass B
  summarizer, `make_summarizer_call` — same model/config as `--collect`'s
  own module-summary pass) → falls back to hard truncation if the call
  fails, throws, or overshoots the budget.
* **Pull-model symbol resolution** — `pull_symbol(name)`: answers a
  `context_request`/`missing_context` symbol from collect's structural
  facts (signature + contracts), the SAME pull channel the coder already
  uses for real source code.

### `tools/auto/context_broker.py`
`ContextBroker` gained a `collect_bridge=None` param and a **Pass 3** in
`resolve()`: any symbol still unresolved after the existing code-search
passes (target files, then whole-project scan) is tried against the
collect model. Code always wins when both have it — collect is a fallback,
never a replacement for real source.

### `tools/auto/inner_loop.py`
`InnerLoop`/`make_inner_loop` accept `collect_bridge`. `run_task()` seeds
`prefetched_context` with the static per-task block (via
`context_for_many(target_files)`) before the first coder attempt, and
passes the same bridge into its `ContextBroker` for pull-model use.

### `tools/auto/outer_loop.py`, `tools/auto/controller.py`
`collect_bridge` threaded through `make_outer_loop`. `AutoController`
builds it **once per run** (`_get_collect_bridge()`, cached by task_mode —
`tools.collect.loader.load()` is called exactly once for N tasks, not N
times) and passes it into `_run_task_loop`'s `make_outer_loop` call. The
old `collect_context_for()` method is kept for backward compatibility and
now delegates to the cached bridge.

### `agents_128k.ini`
Added `[collect] max_context_chars_auto = 1200` with an explanatory
comment on the simplified staleness behaviour.

## Config (unchanged flags, one new one)

```ini
[collect]
use_in_auto            = true   # was false by default — opt in explicitly
staleness               = warn  # warn (default) | refresh | ignore
max_context_chars_auto  = 1200  # NEW — per-task budget before LLM shrink
llm_summaries            = true  # also gates whether the shrink call is available
```

## Tests

* `tests/test_collect_bridge.py` — `CollectBridge` unit tests: staleness
  fallback, budget/shrink/truncation paths, `pull_symbol` matching,
  factory behaviour.
* `tests/test_collect_bridge_wiring.py` — pipeline-level:
  - `ContextBroker` Pass 3 (collect fallback, code-wins-over-collect,
    stale-never-used, `collect_bridge=None` regression).
  - `test_collect_model_loaded_once_per_run_not_per_task` — 3 tasks,
    `loader.load()` called exactly once.
  - `test_use_in_auto_false_prompt_is_byte_for_byte_unchanged` — the
    COLLECT-23 regression AC re-verified after this wiring.
* Existing suite (`tests/test_collect_inject_auto.py`,
  `tests/test_context_broker_*.py`, `test_auto_g*.py`, full `tests/`)
  — all green, no regressions.

Run: `python3 -m pytest tests/ -q` (all pass at commit time).

## Known follow-ups (not in this change)

* `use_in_bughunt` is still unwired (out of scope — only `use_in_auto`
  was requested).
* No automatic `--collect --refresh` is ever triggered from `--auto`;
  the operator decides when to run `--collect` (per product decision).
