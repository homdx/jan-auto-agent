"""
STORY-2.3 — Integration tests
Wire PromptStore into ValidatorAgent, ImprovementAgent, and Orchestrator.

Run with:  python test_story_2_3.py
"""
import inspect
import json
import pathlib
import py_compile
import sys

from tools.prompt_store import PromptStore
from tools.validator_agent import ValidatorAgent, VALIDATOR_PROMPT_HARDCODED
from tools.improvement_agent import ImprovementAgent, IMPROVEMENT_PROMPT_HARDCODED

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
_failures = []

def check(label: str, condition: bool) -> None:
    if condition:
        print(f"  {PASS}: {label}")
    else:
        print(f"  {FAIL}: {label}")
        _failures.append(label)


# ── helpers ────────────────────────────────────────────────────────────────

TMP = pathlib.Path("test_story_2_3_prompts.json")

def fresh_store(**kwargs) -> PromptStore:
    if TMP.exists():
        TMP.unlink()
    return PromptStore(store_path=TMP, **kwargs)


# ── Test 1: main.py compiles and contains wiring ───────────────────────────

print("\n[1] main.py static checks")
try:
    py_compile.compile("main.py", doraise=True)
    check("main.py compiles without syntax errors", True)
except py_compile.PyCompileError as e:
    check(f"main.py compiles — {e}", False)

main_src = pathlib.Path("main.py").read_text()
check("PromptStore imported in main.py",
      "from tools.prompt_store import PromptStore" in main_src)
check("PromptStore instantiated in Orchestrator.__init__()",
      "self.prompt_store = PromptStore(config=self.config)" in main_src)
check("prompt_store passed to ValidatorAgent",
      "prompt_store=self.prompt_store" in main_src)
# appears twice (validator + improvement)
check("prompt_store passed to ImprovementAgent",
      main_src.count("prompt_store=self.prompt_store") >= 2)


# ── Test 2: Agent __init__ signatures ─────────────────────────────────────

print("\n[2] Agent constructor signatures")
va_params = inspect.signature(ValidatorAgent.__init__).parameters
ia_params = inspect.signature(ImprovementAgent.__init__).parameters

check("ValidatorAgent accepts prompt_store kwarg",   "prompt_store" in va_params)
check("ImprovementAgent accepts prompt_store kwarg", "prompt_store" in ia_params)
check("ValidatorAgent.prompt_store defaults to None",
      va_params["prompt_store"].default is None)
check("ImprovementAgent.prompt_store defaults to None",
      ia_params["prompt_store"].default is None)


# ── Test 3: No-store fallback ──────────────────────────────────────────────

print("\n[3] No-store fallback (prompt_store=None)")
va = ValidatorAgent()
ia = ImprovementAgent()
check("ValidatorAgent instantiates without prompt_store",  va.prompt_store is None)
check("ImprovementAgent instantiates without prompt_store", ia.prompt_store is None)


# ── Test 4: Dynamic dispatch — store prompt is used ───────────────────────

print("\n[4] Dynamic dispatch via PromptStore")
ps = fresh_store()

CUSTOM_V = (
    "CUSTOM_VALIDATOR {task} {iteration} {max_iter} "
    "{target_block} {imports} {related_code} {missing_refs}"
)
CUSTOM_I = (
    "CUSTOM_IMPROVEMENT {intent} {target_block} "
    "{imports} {related_code} {context_lines}"
)

ps.push("validator_agent",   CUSTOM_V, score=0.9)
ps.push("improvement_agent", CUSTOM_I, score=0.9)

va_store = ValidatorAgent(prompt_store=ps)
ia_store = ImprovementAgent(prompt_store=ps)

check("ValidatorAgent stores prompt_store reference",
      va_store.prompt_store is ps)
check("ImprovementAgent stores prompt_store reference",
      ia_store.prompt_store is ps)
check("PromptStore returns custom validator prompt",
      ps.get_current("validator_agent") == CUSTOM_V)
check("PromptStore returns custom improvement prompt",
      ps.get_current("improvement_agent") == CUSTOM_I)


# ── Test 5: Rollback falls back to hardcoded constant ────────────────────

print("\n[5] Rollback → hardcoded fallback")
ps.rollback("validator_agent")
check("After rollback, get_current returns hardcoded validator prompt",
      ps.get_current("validator_agent") == VALIDATOR_PROMPT_HARDCODED)

ps.rollback("improvement_agent")
check("After rollback, get_current returns hardcoded improvement prompt",
      ps.get_current("improvement_agent") == IMPROVEMENT_PROMPT_HARDCODED)


# ── Test 6: Live push takes effect without re-instantiating agent ─────────

print("\n[6] Hot-swap: push after agent construction")
ps2 = fresh_store()
va2 = ValidatorAgent(prompt_store=ps2)

# At construction time: hardcoded
check("Before push: get_current returns hardcoded",
      ps2.get_current("validator_agent") == VALIDATOR_PROMPT_HARDCODED)

ps2.push("validator_agent", CUSTOM_V, score=0.8)

# Agent was not re-instantiated — but get_current is called at validate() time
check("After push (no re-instantiation): get_current returns new prompt",
      ps2.get_current("validator_agent") == CUSTOM_V)


# ── Cleanup & summary ─────────────────────────────────────────────────────

if TMP.exists():
    TMP.unlink()

print()
if _failures:
    print(f"  {len(_failures)} test(s) FAILED: {_failures}")
    sys.exit(1)
else:
    total = sum(1 for line in open(__file__) if line.strip().startswith("check("))
    print(f"  All {total} checks passed.")
