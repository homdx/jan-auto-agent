import sys
from pathlib import Path
_here = Path(__file__).resolve().parent
_root = _here.parent if (_here.parent / "tools").exists() else _here
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

"""
STORY-3.2 -- Optimizer trigger logic wired into Orchestrator.
Run with:  python tests/test_story_3_2.py
"""
import py_compile, sys
from unittest.mock import patch

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
_failures = []

def check(label, condition):
    if condition:
        print(f"  {PASS}: {label}")
    else:
        print(f"  {FAIL}: {label}")
        _failures.append(label)

def make_orc():
    from main import Orchestrator
    return Orchestrator(config_path=str(_root / "agents.ini"))

def summary(total=10, avg_iter=1.0, json_fail=0.1, worst="optimize"):
    return {"total_runs": total, "avg_iterations": avg_iter,
            "json_parse_failure_rate": json_fail,
            "common_feedback": [], "worst_intent": worst}

def should_trigger(orc, sv):
    return (
        sv["total_runs"] >= orc.optimizer_min_runs
        and (sv["avg_iterations"] > orc.optimizer_trigger_avg_iter
             or sv["json_parse_failure_rate"] > orc.optimizer_trigger_json_fail)
    )


print("\n[1] main.py static checks")
MAIN = _root / "main.py"
py_compile.compile(str(MAIN), doraise=True)
check("main.py compiles", True)
src = MAIN.read_text()
check("PromptOptimizer imported",         "from tools.prompt_optimizer import PromptOptimizer" in src)
check("PromptOptimizer instantiated",     "self.prompt_optimizer = PromptOptimizer(" in src)
check("optimizer_enabled attr",           "self.optimizer_enabled" in src)
check("optimizer_min_runs attr",          "self.optimizer_min_runs" in src)
check("optimizer_trigger_avg_iter attr",  "self.optimizer_trigger_avg_iter" in src)
check("optimizer_trigger_json_fail attr", "self.optimizer_trigger_json_fail" in src)
check("summarize_failures called",        "summarize_failures" in src)
check("generate_candidate called",        "generate_candidate" in src)
check("evaluate called in run_pipeline",  "self.prompt_evaluator.evaluate" in src)


print("\n[2] Config thresholds from agents.ini")
orc = make_orc()
from tools.prompt_optimizer import PromptOptimizer
check("optimizer_enabled True",           orc.optimizer_enabled is True)
check("optimizer_min_runs == 5",          orc.optimizer_min_runs == 5)
check("optimizer_trigger_avg_iter == 2.0",orc.optimizer_trigger_avg_iter == 2.0)
check("optimizer_trigger_json_fail ~0.30",abs(orc.optimizer_trigger_json_fail - 0.30) < 1e-9)
check("prompt_optimizer is PromptOptimizer", isinstance(orc.prompt_optimizer, PromptOptimizer))


print("\n[3] Trigger fires: avg_iter > threshold")
orc = make_orc()
sv = summary(total=5, avg_iter=2.5, json_fail=0.0)
t = should_trigger(orc, sv)
with patch.object(orc.prompt_optimizer, "generate_candidate", return_value="NEW") as mg:
    with patch.object(orc.prompt_store, "get_current", return_value="OLD"):
        if t:
            candidate = orc.prompt_optimizer.generate_candidate(
                agent_name="validator_agent",
                current_prompt=orc.prompt_store.get_current("validator_agent"),
                failure_summary=sv,
            )
            orc._pending_candidate = ("validator_agent", candidate)
        else:
            orc._pending_candidate = None

check("should_trigger True",           t is True)
check("generate_candidate called",     mg.called)
check("_pending_candidate set",        orc._pending_candidate is not None)
check("agent name correct",            orc._pending_candidate[0] == "validator_agent")
check("candidate is NEW",              orc._pending_candidate[1] == "NEW")
check("called with validator_agent",   "validator_agent" in str(mg.call_args))
check("called with OLD prompt",        "OLD" in str(mg.call_args))


print("\n[4] Trigger fires: json_fail > threshold")
orc = make_orc()
sv2 = summary(total=5, avg_iter=1.0, json_fail=0.5)
t2 = should_trigger(orc, sv2)
with patch.object(orc.prompt_optimizer, "generate_candidate", return_value="C") as mg2:
    with patch.object(orc.prompt_store, "get_current", return_value="X"):
        if t2:
            orc.prompt_optimizer.generate_candidate(
                agent_name="validator_agent",
                current_prompt=orc.prompt_store.get_current("validator_agent"),
                failure_summary=sv2,
            )
check("trigger on high json_fail",     t2 is True)
check("generate_candidate called",     mg2.called)


print("\n[5] Trigger suppressed: below all thresholds")
orc = make_orc()
sv3 = summary(total=5, avg_iter=1.5, json_fail=0.1)
t3 = should_trigger(orc, sv3)
with patch.object(orc.prompt_optimizer, "generate_candidate") as mn:
    if not t3:
        orc._pending_candidate = None
check("should_trigger False",          t3 is False)
check("generate_candidate NOT called", not mn.called)
check("_pending_candidate None",       orc._pending_candidate is None)


print("\n[6] Trigger suppressed: not enough runs")
orc = make_orc()
sv4 = summary(total=2, avg_iter=3.0, json_fail=0.9)
t4 = should_trigger(orc, sv4)
with patch.object(orc.prompt_optimizer, "generate_candidate") as mf:
    if not t4:
        orc._pending_candidate = None
check("trigger False when total_runs < min_runs", t4 is False)
check("generate_candidate NOT called",            not mf.called)


print("\n[7] optimizer_enabled = False")
orc = make_orc()
orc.optimizer_enabled = False
with patch.object(orc.metrics_collector, "summarize_failures") as ms:
    with patch.object(orc.prompt_optimizer, "generate_candidate") as md:
        if orc.optimizer_enabled:
            sv = orc.metrics_collector.summarize_failures(n=10)
            orc.prompt_optimizer.generate_candidate("validator_agent", "", sv)
check("summarize_failures NOT called", not ms.called)
check("generate_candidate NOT called", not md.called)


print()
if _failures:
    print(f"  {len(_failures)} FAILED: {_failures}")
    sys.exit(1)
else:
    total = sum(1 for ln in open(__file__) if ln.strip().startswith("check("))
    print(f"  All {total} checks passed.")
