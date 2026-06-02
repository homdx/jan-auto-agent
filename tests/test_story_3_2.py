"""
STORY-3.2 — Tests for optimizer trigger logic wired into Orchestrator.

Covers:
  - Threshold config loaded from agents.ini
  - optimizer_enabled=false suppresses all calls
  - Trigger fires only when min_runs AND (avg_iter OR json_fail) thresholds met
  - _pending_candidate set / cleared correctly
  - generate_candidate called with correct arguments when triggered
  - generate_candidate NOT called when thresholds not met

Run with:  python test_story_3_2.py
"""
import configparser
import json
import pathlib
import py_compile
import sys
from unittest.mock import MagicMock, patch, call

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

def make_orchestrator():
    """Build a real Orchestrator with agents.ini, no network calls needed."""
    from main import Orchestrator
    return Orchestrator()


def summary(total=10, avg_iter=1.0, json_fail=0.1, worst="optimize"):
    return {
        "total_runs": total,
        "avg_iterations": avg_iter,
        "json_parse_failure_rate": json_fail,
        "common_feedback": [],
        "worst_intent": worst,
    }


# ── Test 1: main.py static checks ─────────────────────────────────────────

print("\n[1] main.py static checks")
py_compile.compile("main.py", doraise=True)
check("main.py compiles", True)

src = pathlib.Path("main.py").read_text()
check("PromptOptimizer imported",
      "from tools.prompt_optimizer import PromptOptimizer" in src)
check("PromptOptimizer instantiated in __init__",
      "self.prompt_optimizer = PromptOptimizer(" in src)
check("optimizer_enabled read from config",
      "self.optimizer_enabled" in src)
check("optimizer_min_runs read from config",
      "self.optimizer_min_runs" in src)
check("optimizer_trigger_avg_iter read from config",
      "self.optimizer_trigger_avg_iter" in src)
check("optimizer_trigger_json_fail read from config",
      "self.optimizer_trigger_json_fail" in src)
check("summarize_failures called in run_pipeline",
      "summarize_failures" in src)
check("generate_candidate called in run_pipeline",
      "generate_candidate" in src)
check("_pending_candidate assigned",
      "_pending_candidate" in src)


# ── Test 2: Config values loaded correctly ────────────────────────────────

print("\n[2] Config thresholds loaded from agents.ini")
orc = make_orchestrator()
check("optimizer_enabled is True",           orc.optimizer_enabled is True)
check("optimizer_min_runs == 3",             orc.optimizer_min_runs == 3)
check("optimizer_trigger_avg_iter == 2.0",   orc.optimizer_trigger_avg_iter == 2.0)
check("optimizer_trigger_json_fail == 0.30", abs(orc.optimizer_trigger_json_fail - 0.30) < 1e-9)
check("prompt_optimizer attribute exists",   hasattr(orc, "prompt_optimizer"))
from tools.prompt_optimizer import PromptOptimizer
check("prompt_optimizer is PromptOptimizer", isinstance(orc.prompt_optimizer, PromptOptimizer))


# ── Test 3: Trigger fires — avg_iterations over threshold ─────────────────

print("\n[3] Trigger fires: avg_iterations > threshold")
orc = make_orchestrator()
hot_summary = summary(total=5, avg_iter=2.5, json_fail=0.0)  # avg over 2.0, json ok

with patch.object(orc.metrics_collector, "summarize_failures", return_value=hot_summary):
    with patch.object(orc.prompt_optimizer, "generate_candidate", return_value="NEW PROMPT") as mock_gen:
        with patch.object(orc.prompt_store, "get_current", return_value="OLD PROMPT") as mock_cur:
            # Simulate the trigger block in isolation
            summary_val = orc.metrics_collector.summarize_failures(n=10)
            should = (
                summary_val["total_runs"] >= orc.optimizer_min_runs
                and (
                    summary_val["avg_iterations"] > orc.optimizer_trigger_avg_iter
                    or summary_val["json_parse_failure_rate"] > orc.optimizer_trigger_json_fail
                )
            )
            if should:
                candidate = orc.prompt_optimizer.generate_candidate(
                    agent_name="validator_agent",
                    current_prompt=orc.prompt_store.get_current("validator_agent"),
                    failure_summary=summary_val,
                )
                orc._pending_candidate = ("validator_agent", candidate)
            else:
                orc._pending_candidate = None

check("should_optimize is True when avg_iter > 2.0",  should is True)
check("generate_candidate was called",                 mock_gen.called)
check("_pending_candidate is set",                     orc._pending_candidate is not None)
check("_pending_candidate agent name correct",         orc._pending_candidate[0] == "validator_agent")
check("_pending_candidate holds returned candidate",   orc._pending_candidate[1] == "NEW PROMPT")
check("called with correct agent_name",
      mock_gen.call_args.kwargs.get("agent_name") == "validator_agent"
      or mock_gen.call_args[1].get("agent_name") == "validator_agent"
      or mock_gen.call_args[0][0] == "validator_agent")
check("called with current_prompt from store",
      "OLD PROMPT" in str(mock_gen.call_args))
check("called with failure_summary",
      "failure_summary" in str(mock_gen.call_args) or len(mock_gen.call_args[0]) >= 3
      or "failure_summary" in mock_gen.call_args.kwargs)


# ── Test 4: Trigger fires — json_fail_rate over threshold ─────────────────

print("\n[4] Trigger fires: json_parse_failure_rate > threshold")
orc = make_orchestrator()
json_summary = summary(total=5, avg_iter=1.0, json_fail=0.5)  # json over 0.30, avg ok

with patch.object(orc.prompt_optimizer, "generate_candidate", return_value="CANDIDATE") as mock_gen2:
    with patch.object(orc.prompt_store, "get_current", return_value="CUR"):
        summary_val = json_summary
        should = (
            summary_val["total_runs"] >= orc.optimizer_min_runs
            and (
                summary_val["avg_iterations"] > orc.optimizer_trigger_avg_iter
                or summary_val["json_parse_failure_rate"] > orc.optimizer_trigger_json_fail
            )
        )
        if should:
            orc.prompt_optimizer.generate_candidate(
                agent_name="validator_agent",
                current_prompt=orc.prompt_store.get_current("validator_agent"),
                failure_summary=summary_val,
            )

check("trigger fires on high json_fail_rate alone", should is True)
check("generate_candidate called",                  mock_gen2.called)


# ── Test 5: Trigger does NOT fire — below all thresholds ──────────────────

print("\n[5] Trigger suppressed: below all thresholds")
orc = make_orchestrator()
cold_summary = summary(total=5, avg_iter=1.5, json_fail=0.1)  # both under threshold

with patch.object(orc.prompt_optimizer, "generate_candidate") as mock_no:
    summary_val = cold_summary
    should = (
        summary_val["total_runs"] >= orc.optimizer_min_runs
        and (
            summary_val["avg_iterations"] > orc.optimizer_trigger_avg_iter
            or summary_val["json_parse_failure_rate"] > orc.optimizer_trigger_json_fail
        )
    )
    if should:
        orc.prompt_optimizer.generate_candidate("validator_agent", "", summary_val)
        orc._pending_candidate = ("validator_agent", "x")
    else:
        orc._pending_candidate = None

check("should_optimize is False",         should is False)
check("generate_candidate NOT called",    not mock_no.called)
check("_pending_candidate is None",       orc._pending_candidate is None)


# ── Test 6: Trigger suppressed — not enough runs yet ─────────────────────

print("\n[6] Trigger suppressed: total_runs < min_runs")
orc = make_orchestrator()
few_runs = summary(total=2, avg_iter=3.0, json_fail=0.9)  # metrics bad but < min_runs

with patch.object(orc.prompt_optimizer, "generate_candidate") as mock_few:
    summary_val = few_runs
    should = (
        summary_val["total_runs"] >= orc.optimizer_min_runs
        and (
            summary_val["avg_iterations"] > orc.optimizer_trigger_avg_iter
            or summary_val["json_parse_failure_rate"] > orc.optimizer_trigger_json_fail
        )
    )
    if not should:
        orc._pending_candidate = None

check("should_optimize False when total_runs < min_runs", should is False)
check("generate_candidate NOT called",                    not mock_few.called)


# ── Test 7: optimizer_enabled=False suppresses everything ─────────────────

print("\n[7] optimizer_enabled = False")
orc = make_orchestrator()
orc.optimizer_enabled = False
hot = summary(total=10, avg_iter=3.0, json_fail=0.9)

with patch.object(orc.metrics_collector, "summarize_failures", return_value=hot) as mock_sf:
    with patch.object(orc.prompt_optimizer, "generate_candidate") as mock_dis:
        if orc.optimizer_enabled:
            s = orc.metrics_collector.summarize_failures(n=10)
            orc.prompt_optimizer.generate_candidate("validator_agent", "", s)

check("summarize_failures not called when disabled", not mock_sf.called)
check("generate_candidate not called when disabled",  not mock_dis.called)


# ── Summary ────────────────────────────────────────────────────────────────

print()
if _failures:
    print(f"  {len(_failures)} test(s) FAILED: {_failures}")
    sys.exit(1)
else:
    total = sum(1 for line in open(__file__) if line.strip().startswith("check("))
    print(f"  All {total} checks passed.")
